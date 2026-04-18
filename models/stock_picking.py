# -*- coding: utf-8 -*-
import logging
import json
from odoo import models
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating: propagate lots to downstream moves and backorders."""
        validating_ids = self.ids
        res = super().button_validate()

        for picking in self.browse(validating_ids):
            if picking.state != 'done':
                continue
            picking._propagate_whole_lots_to_next_step()
            picking._assign_whole_lots_to_backorder()

        return res

    # ═══════════════════════════════════════════════════════════════════════════
    # PROPAGACIÓN A SIGUIENTE PASO
    # ═══════════════════════════════════════════════════════════════════════════

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
            "WholeLot: Post-validation propagation from %s -> %d deferred move(s)",
            self.name, len(deferred_moves)
        )

        deferred_moves.with_context(skip_whole_lot_strategy=True)._action_assign()

    # ═══════════════════════════════════════════════════════════════════════════
    # ASIGNACIÓN AL BACKORDER
    # ═══════════════════════════════════════════════════════════════════════════

    def _assign_whole_lots_to_backorder(self):
        """Force-assign pending lots to backorder. Respeta whole_lot y whole_lot_partial."""
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
                    strategy = move._get_whole_lot_strategy_type()
                    if not strategy:
                        continue
                except Exception:
                    continue

                product = move.product_id
                rounding = product.uom_id.rounding
                sol = move.sale_line_id
                if not sol:
                    continue

                selection = move._get_sol_lot_selection(sol)
                all_so_lot_ids = selection['lot_ids']
                breakdown = selection['breakdown']

                if not all_so_lot_ids:
                    continue

                existing_so_lots = self.env['stock.lot'].browse(list(all_so_lot_ids)).exists()
                removed_ids = all_so_lot_ids - set(existing_so_lots.ids)
                if removed_ids:
                    _logger.warning(
                        "WholeLot: Filtered out %d non-existent lot IDs: %s",
                        len(removed_ids), list(removed_ids)
                    )
                    all_so_lot_ids = set(existing_so_lots.ids)

                if not all_so_lot_ids:
                    continue

                all_delivered_ids = set()
                done_moves = sol.move_ids.filtered(lambda m: m.state == 'done')
                for dm in done_moves:
                    for ml in dm.move_line_ids:
                        if ml.lot_id:
                            all_delivered_ids.add(ml.lot_id.id)

                pending_lot_ids = all_so_lot_ids - all_delivered_ids

                _logger.info(
                    "WholeLot[%s]: Backorder %s move %d - SO: %s, Delivered: %s, Pending: %s",
                    strategy, bo_picking.name, move.id,
                    list(all_so_lot_ids), list(all_delivered_ids), list(pending_lot_ids)
                )

                if not pending_lot_ids:
                    continue

                existing_lot_ids = set(
                    ml.lot_id.id for ml in move.move_line_ids if ml.lot_id
                )
                if existing_lot_ids == pending_lot_ids:
                    if strategy == 'whole_lot_partial' and breakdown:
                        qtys_match = all(
                            float_compare(
                                ml.quantity if 'quantity' in ml._fields else 0.0,
                                breakdown.get(ml.lot_id.id, 0.0),
                                precision_rounding=rounding
                            ) == 0
                            for ml in move.move_line_ids if ml.lot_id
                        )
                        if qtys_match:
                            _logger.info("WholeLot: Backorder already has correct lots+qtys, skipping")
                            continue
                    else:
                        _logger.info("WholeLot: Backorder already has correct lots, skipping")
                        continue

                if move.move_line_ids:
                    _logger.info(
                        "WholeLot: Clearing %d incorrect move_lines", len(move.move_line_ids)
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

                pending_lots = self.env['stock.lot'].browse(list(pending_lot_ids)).exists()
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
                    positive_quants = quants.filtered(lambda q: q.quantity > 0)
                    lot_real_qty = sum(q.quantity for q in positive_quants) if positive_quants else 0.0

                    _logger.info(
                        "WholeLot: Lot %s - total: %.2f, reserved: %.2f, available: %.2f, real_qty: %.2f",
                        lot.name, lot_total_qty, lot_reserved_qty,
                        lot_available_qty, lot_real_qty
                    )

                    if strategy == 'whole_lot_partial' and lot.id in breakdown:
                        desired_qty = breakdown[lot.id]
                        max_available = max(lot_real_qty, lot_total_qty, lot_reserved_qty)
                        reserve_qty = min(desired_qty, max_available)
                        _logger.info(
                            "WholeLot[partial]: Lot %s - breakdown says %.2f, reserving %.2f",
                            lot.name, desired_qty, reserve_qty
                        )
                    else:
                        if float_compare(lot_real_qty, 0, precision_rounding=rounding) > 0:
                            reserve_qty = lot_real_qty
                        elif float_compare(lot_total_qty, 0, precision_rounding=rounding) > 0:
                            reserve_qty = lot_total_qty
                        elif float_compare(lot_reserved_qty, 0, precision_rounding=rounding) > 0:
                            reserve_qty = lot_reserved_qty
                        else:
                            _logger.warning(
                                "WholeLot: Lot %s has zero quantity, skipping", lot.name
                            )
                            continue

                    if float_compare(reserve_qty, 0, precision_rounding=rounding) <= 0:
                        continue

                    if float_compare(lot_available_qty, 0, precision_rounding=rounding) > 0:
                        try:
                            qty_to_reserve = min(reserve_qty, lot_available_qty)
                            Quant._update_reserved_quantity(
                                product, move.location_id, qty_to_reserve,
                                lot_id=lot, strict=False
                            )
                        except Exception as e:
                            _logger.error("WholeLot: Failed to reserve lot %s: %s", lot.name, e)
                            continue
                    else:
                        _logger.info(
                            "WholeLot: Lot %s already reserved (backorder transfer)", lot.name
                        )

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

                    first_quant = quants[0]
                    if first_quant.package_id:
                        ml_vals['package_id'] = first_quant.package_id.id
                    if first_quant.owner_id:
                        ml_vals['owner_id'] = first_quant.owner_id.id

                    self.env['stock.move.line'].create(ml_vals)
                    total_reserved += reserve_qty

                    _logger.info(
                        "WholeLot[%s]: SUCCESS - Assigned lot '%s' (%.2f %s) to backorder %s",
                        strategy, lot.name, reserve_qty, product.uom_id.name, bo_picking.name
                    )

                if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                    total_demand = move.product_uom._compute_quantity(
                        move.product_uom_qty, product.uom_id,
                        rounding_method='HALF-UP'
                    )
                    if float_compare(total_reserved, total_demand, precision_rounding=rounding) >= 0:
                        move.write({'state': 'assigned'})
                    else:
                        move.write({'state': 'partially_available'})

                _logger.info(
                    "WholeLot: Backorder move %d final: state=%s, lots=%s",
                    move.id, move.state,
                    [(ml.lot_id.name, ml.quantity)
                     for ml in move.move_line_ids if ml.lot_id]
                )
