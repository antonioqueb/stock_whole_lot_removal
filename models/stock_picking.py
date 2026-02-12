# -*- coding: utf-8 -*-
import logging
import json
from odoo import models
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating a picking, propagate lots to:
        1. Downstream moves (OUT step) that were deferred
        2. Backorder PICK moves - force correct lot assignment
        """
        validating_ids = self.ids
        res = super().button_validate()

        for picking in self.browse(validating_ids):
            if picking.state != 'done':
                continue
            picking._propagate_whole_lots_to_next_step()
            picking._assign_whole_lots_to_backorder()

        return res

    def _propagate_whole_lots_to_next_step(self):
        """Find downstream moves that were deferred and assign them."""
        self.ensure_one()

        next_moves = self.env['stock.move']
        for move in self.move_ids:
            if move.state == 'done':
                next_moves |= move.move_dest_ids

        if not next_moves:
            return

        deferred_moves = self.env['stock.move']
        for move in next_moves:
            if move.state not in ('confirmed', 'partially_available', 'waiting'):
                continue
            try:
                if move._should_use_whole_lot_strategy():
                    deferred_moves |= move
            except Exception:
                continue

        if not deferred_moves:
            return

        _logger.info(
            "WholeLot: Post-validation propagation from %s -> %d deferred move(s): %s",
            self.name, len(deferred_moves),
            [(m.id, m.product_id.display_name, m.picking_id.name if m.picking_id else 'N/A')
             for m in deferred_moves]
        )

        deferred_moves.with_context(skip_whole_lot_strategy=True)._action_assign()

        for move in deferred_moves:
            _logger.info(
                "WholeLot: Post-propagation result: move %d state=%s, "
                "move_lines=%d, lots=%s (picking %s)",
                move.id, move.state,
                len(move.move_line_ids),
                [(ml.lot_id.name, ml.quantity)
                 for ml in move.move_line_ids if ml.lot_id],
                move.picking_id.name if move.picking_id else 'N/A'
            )

    def _assign_whole_lots_to_backorder(self):
        """Force-assign correct pending lots to backorder PICK.

        WHY THIS IS NEEDED:
        When button_validate creates a backorder, Odoo transfers reservations
        from the original PICK to the backorder. But within the same transaction,
        _get_whole_lot_available_quants can't see the pending lots as "available"
        because their reserved_quantity is still > 0.

        APPROACH:
        Don't search for "available" quants. Instead:
        1. Calculate which lots were delivered vs which the SO needs
        2. Pending = SO lots - delivered lots
        3. Read the quant quantities directly (ignoring reserved_quantity)
        4. Create move_lines that claim the already-transferred reservations
        """
        self.ensure_one()

        backorder_pickings = self.env['stock.picking'].search([
            ('backorder_id', '=', self.id),
            ('state', 'in', ('confirmed', 'waiting', 'assigned')),
        ])

        if not backorder_pickings:
            return

        Quant = self.env['stock.quant']

        for bo_picking in backorder_pickings:
            for move in bo_picking.move_ids:
                if move.state not in ('confirmed', 'partially_available', 'waiting', 'assigned'):
                    continue
                try:
                    if not move._should_use_whole_lot_strategy():
                        continue
                except Exception:
                    continue

                product = move.product_id
                rounding = product.uom_id.rounding
                sol = move.sale_line_id

                if not sol:
                    continue

                # ─── Collect ALL lots the SO requires ───
                all_so_lot_ids = set()

                if hasattr(sol, 'x_lot_breakdown_json') and sol.x_lot_breakdown_json:
                    try:
                        json_data = sol.x_lot_breakdown_json
                        if isinstance(json_data, str):
                            json_data = json.loads(json_data)
                        if isinstance(json_data, dict):
                            all_so_lot_ids.update(
                                int(k) for k in json_data.keys() if k.isdigit()
                            )
                    except Exception:
                        pass

                if hasattr(sol, 'x_selected_lots') and sol.x_selected_lots:
                    all_so_lot_ids.update(
                        sol.x_selected_lots.mapped('lot_id').ids
                    )

                if hasattr(sol, 'lot_ids') and sol.lot_ids:
                    all_so_lot_ids.update(sol.lot_ids.ids)

                if not all_so_lot_ids:
                    continue

                # ─── Collect ALL delivered lots across all done moves ───
                all_delivered_ids = set()
                done_moves = sol.move_ids.filtered(lambda m: m.state == 'done')
                for dm in done_moves:
                    for ml in dm.move_line_ids:
                        if ml.lot_id:
                            all_delivered_ids.add(ml.lot_id.id)

                # Pending = what SO needs minus what's delivered
                pending_lot_ids = all_so_lot_ids - all_delivered_ids

                _logger.info(
                    "WholeLot: Backorder %s move %d - SO lots: %s, "
                    "Delivered: %s, Pending: %s",
                    bo_picking.name, move.id,
                    list(all_so_lot_ids), list(all_delivered_ids),
                    list(pending_lot_ids)
                )

                if not pending_lot_ids:
                    _logger.info(
                        "WholeLot: All SO lots already delivered, nothing for backorder"
                    )
                    continue

                # ─── Check if backorder already has correct lots ───
                existing_lot_ids = set(
                    ml.lot_id.id for ml in move.move_line_ids if ml.lot_id
                )
                if existing_lot_ids == pending_lot_ids:
                    _logger.info(
                        "WholeLot: Backorder already has correct lots, skipping"
                    )
                    continue

                # ─── Clear incorrect move_lines ───
                if move.move_line_ids:
                    _logger.info(
                        "WholeLot: Clearing %d incorrect move_lines (lots: %s)",
                        len(move.move_line_ids),
                        [ml.lot_id.name for ml in move.move_line_ids if ml.lot_id]
                    )
                    for ml in move.move_line_ids:
                        ml_qty = ml.quantity if 'quantity' in ml._fields else 0.0
                        if ml_qty > 0 and ml.lot_id:
                            try:
                                Quant._update_reserved_quantity(
                                    product, move.location_id, -ml_qty,
                                    lot_id=ml.lot_id, strict=False
                                )
                            except Exception:
                                pass
                    move.move_line_ids.unlink()

                # ─── Force-assign pending lots ───
                pending_lots = self.env['stock.lot'].browse(list(pending_lot_ids))
                total_reserved = 0.0

                for lot in pending_lots:
                    quants = Quant._gather(
                        product, move.location_id, lot_id=lot, strict=False
                    )

                    if not quants:
                        _logger.warning(
                            "WholeLot: No quants for lot %s at %s",
                            lot.name, move.location_id.complete_name
                        )
                        continue

                    lot_total_qty = sum(q.quantity for q in quants)
                    lot_reserved_qty = sum(q.reserved_quantity for q in quants)
                    lot_available_qty = lot_total_qty - lot_reserved_qty

                    # For quants with quantity > 0 specifically (ignore residual quants)
                    positive_quants = quants.filtered(lambda q: q.quantity > 0)
                    lot_real_qty = sum(q.quantity for q in positive_quants) if positive_quants else 0.0

                    _logger.info(
                        "WholeLot: Lot %s quant info - total: %.2f, "
                        "reserved: %.2f, available: %.2f, real_qty: %.2f, "
                        "quant_count: %d",
                        lot.name, lot_total_qty, lot_reserved_qty,
                        lot_available_qty, lot_real_qty, len(quants)
                    )

                    # Determine the actual quantity of this lot
                    # Priority: real_qty (from positive quants) > lot_total_qty > lot_reserved_qty
                    if float_compare(lot_real_qty, 0, precision_rounding=rounding) > 0:
                        reserve_qty = lot_real_qty
                    elif float_compare(lot_total_qty, 0, precision_rounding=rounding) > 0:
                        reserve_qty = lot_total_qty
                    elif float_compare(lot_reserved_qty, 0, precision_rounding=rounding) > 0:
                        # Quant has quantity=0 but reserved>0 — this is a residual
                        # quant from Odoo's backorder transfer. The reservation IS
                        # ours. Use reserved_qty as the lot quantity.
                        reserve_qty = lot_reserved_qty
                    else:
                        _logger.warning(
                            "WholeLot: Lot %s has zero quantity everywhere, skipping",
                            lot.name
                        )
                        continue

                    if float_compare(lot_available_qty, 0, precision_rounding=rounding) > 0:
                        # Available: reserve it normally
                        try:
                            Quant._update_reserved_quantity(
                                product, move.location_id, lot_available_qty,
                                lot_id=lot, strict=False
                            )
                        except Exception as e:
                            _logger.error(
                                "WholeLot: Failed to reserve lot %s: %s",
                                lot.name, e
                            )
                            continue
                    else:
                        # Already reserved (by Odoo's backorder transfer) or
                        # residual quant — just create the move_line
                        _logger.info(
                            "WholeLot: Lot %s already reserved (backorder transfer), "
                            "creating move_line with qty %.2f",
                            lot.name, reserve_qty
                        )
                        pass

                    # Create move line
                    uom_qty = product.uom_id._compute_quantity(
                        reserve_qty, move.product_uom, rounding_method='HALF-UP'
                    )

                    ml_vals = {
                        'move_id': move.id,
                        'product_id': product.id,
                        'product_uom_id': move.product_uom.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'lot_id': lot.id,
                        'lot_name': lot.name,
                        'picking_id': bo_picking.id,
                        'company_id': move.company_id.id or self.env.company.id,
                    }

                    if 'reserved_uom_qty' in self.env['stock.move.line']._fields:
                        ml_vals['reserved_uom_qty'] = uom_qty
                    else:
                        ml_vals['quantity'] = uom_qty

                    # Copy package/owner from quant if present
                    first_quant = quants[0]
                    if first_quant.package_id:
                        ml_vals['package_id'] = first_quant.package_id.id
                    if first_quant.owner_id:
                        ml_vals['owner_id'] = first_quant.owner_id.id

                    self.env['stock.move.line'].create(ml_vals)
                    total_reserved += reserve_qty

                    _logger.info(
                        "WholeLot: SUCCESS - Assigned lot '%s' (%.2f %s) "
                        "to backorder %s",
                        lot.name, reserve_qty, product.uom_id.name,
                        bo_picking.name
                    )

                # Update move state
                if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                    total_demand = move.product_uom._compute_quantity(
                        move.product_uom_qty, product.uom_id,
                        rounding_method='HALF-UP'
                    )
                    if float_compare(
                        total_reserved, total_demand,
                        precision_rounding=rounding
                    ) >= 0:
                        move.write({'state': 'assigned'})
                    else:
                        move.write({'state': 'partially_available'})

                _logger.info(
                    "WholeLot: Backorder move %d final: state=%s, lots=%s",
                    move.id, move.state,
                    [(ml.lot_id.name, ml.quantity)
                     for ml in move.move_line_ids if ml.lot_id]
                )

    def _get_all_sale_lots_with_qty(self):
        """
        Retorna TODOS los lotes de la venta con su cantidad,
        buscando en todos los moves/pickings + fallback a lot_ids.
        Para uso en reportes.
        """
        self.ensure_one()
        
        # 1. Buscar en TODOS los stock.move.line vinculados
        move_lines = self.env['stock.move.line'].search([
            ('move_id.sale_line_id', '=', self.id),
            ('lot_id', '!=', False),
        ])
        
        if move_lines:
            lot_data = {}
            for ml in move_lines:
                lot = ml.lot_id
                if lot.id not in lot_data:
                    lot_data[lot.id] = {'lot': lot, 'quantity': 0.0}
                lot_data[lot.id]['quantity'] += ml.quantity or ml.reserved_uom_qty or 0.0
            return list(lot_data.values())
        
        # 2. Fallback: lot_ids (pre-confirmación o sin moves aún)
        if self.lot_ids:
            result = []
            for lot in self.lot_ids:
                quant = self.env['stock.quant'].search([
                    ('lot_id', '=', lot.id),
                    ('product_id', '=', self.product_id.id),
                    ('location_id.usage', '=', 'internal'),
                    ('quantity', '>', 0)
                ], limit=1)
                result.append({
                    'lot': lot,
                    'quantity': quant.quantity if quant else (lot.x_alto * lot.x_ancho if lot.x_alto and lot.x_ancho else 0.0),
                })
            return result
        
        return []