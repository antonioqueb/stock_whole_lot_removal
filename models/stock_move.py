# -*- coding: utf-8 -*-
import logging
from odoo import models, api, _
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _should_use_whole_lot_strategy(self):
        """Determine if this move should use the whole_lot removal strategy."""
        self.ensure_one()
        product = self.product_id
        if product.tracking not in ('lot', 'serial'):
            return False

        # Check product category first (has priority)
        if product.categ_id.removal_strategy_id and \
                product.categ_id.removal_strategy_id.method == 'whole_lot':
            return True

        # Then check location hierarchy
        location = self.location_id
        while location:
            if location.removal_strategy_id and \
                    location.removal_strategy_id.method == 'whole_lot':
                return True
            location = location.location_id
        return False

    def _action_assign(self, force_qty=False):
        """Override to intercept moves that use whole_lot removal strategy."""
        whole_lot_moves = self.env['stock.move']
        whole_lot_deferred = self.env['stock.move']
        regular_moves = self.env['stock.move']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting') \
                    or not move._should_use_whole_lot_strategy():
                regular_moves |= move
                continue

            if move.move_orig_ids:
                whole_lot_deferred |= move
                _logger.info(
                    "WholeLot: Deferring reservation for %s (picking %s) - "
                    "has %d origin move(s), states: %s",
                    move.product_id.display_name,
                    move.picking_id.name if move.picking_id else 'N/A',
                    len(move.move_orig_ids),
                    [m.state for m in move.move_orig_ids]
                )
            else:
                whole_lot_moves |= move

        # Process regular moves with standard Odoo logic
        if regular_moves:
            super(StockMove, regular_moves)._action_assign(force_qty=force_qty)

        # Process whole-lot moves WITHOUT origin (single-step or first step)
        if whole_lot_moves:
            whole_lot_moves._assign_whole_lots()

        return True

    def _get_reserved_qty(self, move):
        """Get the currently reserved quantity for a move in its product UoM."""
        if hasattr(move, 'forecast_availability'):
            reserved = 0.0
            for ml in move.move_line_ids:
                if hasattr(ml, 'reserved_uom_qty'):
                    reserved += ml.product_uom_id._compute_quantity(
                        ml.reserved_uom_qty, move.product_id.uom_id,
                        rounding_method='HALF-UP'
                    )
                elif hasattr(ml, 'reserved_qty'):
                    reserved += ml.reserved_qty
                elif hasattr(ml, 'product_uom_qty'):
                    reserved += ml.product_uom_id._compute_quantity(
                        ml.product_uom_qty, move.product_id.uom_id,
                        rounding_method='HALF-UP'
                    )
            return reserved
        if hasattr(move, 'reserved_availability'):
            return move.reserved_availability
        return 0.0

    def _assign_whole_lots(self):
        """Reserve stock using whole-lot strategy."""
        Quant = self.env['stock.quant']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting'):
                continue

            product = move.product_id
            rounding = product.uom_id.rounding

            total_demand_move_uom = move.product_uom_qty
            total_demand = move.product_uom._compute_quantity(
                total_demand_move_uom, product.uom_id, rounding_method='HALF-UP'
            )

            already_reserved = self._get_reserved_qty(move)
            need = total_demand - already_reserved

            if float_is_zero(need, precision_rounding=rounding) or \
                    float_compare(need, 0, precision_rounding=rounding) <= 0:
                continue

            available_lots = Quant._get_whole_lot_available_quants(
                product, move.location_id
            )

            if not available_lots:
                _logger.info(
                    "WholeLot: No lots available for %s at %s",
                    product.display_name, move.location_id.complete_name
                )
                continue

            selected = Quant._whole_lot_select_lots(available_lots, need, rounding)

            if not selected:
                _logger.info(
                    "WholeLot: Cannot select complete lots for demand %.2f %s of %s. "
                    "Available: %s",
                    need, product.uom_id.name, product.display_name,
                    [(d['lot_id'].name, d['available_qty']) for d in available_lots]
                )
                continue

            total_reserved = 0.0
            for lot_data in selected:
                lot = lot_data['lot_id']
                qty = lot_data['available_qty']

                if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                    continue

                try:
                    reserved = Quant._update_reserved_quantity(
                        product, move.location_id, qty,
                        lot_id=lot, strict=False
                    )

                    if isinstance(reserved, (int, float)):
                        actual_reserved = reserved
                    else:
                        actual_reserved = sum(r[1] for r in reserved) if reserved else 0

                    if float_compare(actual_reserved, 0, precision_rounding=rounding) > 0:
                        self._create_whole_lot_move_line(
                            move, lot, actual_reserved, product
                        )
                        total_reserved += actual_reserved
                        _logger.info(
                            "WholeLot: Reserved lot '%s' (%.2f %s) for %s",
                            lot.name, actual_reserved, product.uom_id.name,
                            product.display_name
                        )
                except Exception as e:
                    _logger.warning(
                        "WholeLot: Failed to reserve lot '%s' for %s: %s",
                        lot.name if lot else 'N/A', product.display_name, str(e)
                    )
                    continue

            if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                new_reserved = self._get_reserved_qty(move)
                cmp = float_compare(
                    new_reserved, total_demand,
                    precision_rounding=rounding
                )
                if cmp >= 0:
                    move.write({'state': 'assigned'})
                elif move.state != 'partially_available':
                    move.write({'state': 'partially_available'})

                shortfall = need - total_reserved
                if float_compare(shortfall, 0, precision_rounding=rounding) > 0:
                    _logger.info(
                        "WholeLot: Partially fulfilled %.2f/%.2f %s for %s. "
                        "%.2f %s pending manual selection.",
                        total_reserved, need, product.uom_id.name,
                        product.display_name, shortfall, product.uom_id.name
                    )

    def _assign_whole_lots_from_origin(self):
        """Assign lots to moves based on their origin moves (multi-step propagation).

        Called AFTER the origin picking has been fully validated and quants
        have been moved to the intermediate location.
        """
        Quant = self.env['stock.quant']

        _logger.info(
            "WholeLot: === _assign_whole_lots_from_origin START === "
            "move count=%d, IDs=%s",
            len(self), self.ids
        )

        for move in self:
            _logger.info(
                "WholeLot: [PROP] ---- Move ID %d ---- product=%s, state=%s, "
                "location_id=%s (ID:%d), picking=%s",
                move.id, move.product_id.display_name, move.state,
                move.location_id.complete_name, move.location_id.id,
                move.picking_id.name if move.picking_id else 'N/A'
            )

            if move.state not in ('confirmed', 'partially_available', 'waiting'):
                _logger.info(
                    "WholeLot: [PROP] SKIP move %d - state '%s' not actionable",
                    move.id, move.state
                )
                continue

            product = move.product_id
            rounding = product.uom_id.rounding

            total_demand_move_uom = move.product_uom_qty
            total_demand = move.product_uom._compute_quantity(
                total_demand_move_uom, product.uom_id, rounding_method='HALF-UP'
            )

            already_reserved = self._get_reserved_qty(move)
            need = total_demand - already_reserved

            _logger.info(
                "WholeLot: [PROP] demand=%.4f, already_reserved=%.4f, need=%.4f",
                total_demand, already_reserved, need
            )

            if float_is_zero(need, precision_rounding=rounding) or \
                    float_compare(need, 0, precision_rounding=rounding) <= 0:
                _logger.info("WholeLot: [PROP] SKIP - need <= 0 (already fulfilled)")
                continue

            # ---- Collect lots from origin moves ----
            lot_assignments = []
            _logger.info(
                "WholeLot: [PROP] Origin moves: %d, IDs=%s",
                len(move.move_orig_ids), move.move_orig_ids.ids
            )
            for orig_move in move.move_orig_ids:
                _logger.info(
                    "WholeLot: [PROP] Origin move ID %d: state=%s, "
                    "move_line_ids=%d, product=%s",
                    orig_move.id, orig_move.state,
                    len(orig_move.move_line_ids),
                    orig_move.product_id.display_name
                )
                if orig_move.state != 'done':
                    continue
                for ml in orig_move.move_line_ids:
                    _logger.info(
                        "WholeLot: [PROP]   ML ID %d: lot=%s (ID:%s), "
                        "qty=%.4f, qty_done_field=%s, "
                        "loc_dest=%s (ID:%d)",
                        ml.id,
                        ml.lot_id.name if ml.lot_id else 'NONE',
                        ml.lot_id.id if ml.lot_id else 'N/A',
                        ml.quantity,
                        getattr(ml, 'qty_done', 'N/A'),
                        ml.location_dest_id.complete_name,
                        ml.location_dest_id.id
                    )
                    if ml.lot_id and float_compare(
                        ml.quantity, 0, precision_rounding=rounding
                    ) > 0:
                        lot_assignments.append({
                            'lot_id': ml.lot_id,
                            'qty': ml.quantity,
                        })

            if not lot_assignments:
                _logger.info(
                    "WholeLot: [PROP] NO origin lots found, falling back to standard"
                )
                move._assign_whole_lots()
                continue

            _logger.info(
                "WholeLot: [PROP] Found %d lot(s) to propagate: %s",
                len(lot_assignments),
                [(la['lot_id'].name, la['qty']) for la in lot_assignments]
            )

            # ---- Debug: dump ALL quants at source location ----
            all_quants = Quant.sudo().search([
                ('product_id', '=', product.id),
                ('location_id', 'child_of', move.location_id.id),
                ('quantity', '!=', 0),
            ])
            _logger.info(
                "WholeLot: [PROP] ALL quants (sudo search) for product %s "
                "at/under %s (ID:%d): count=%d",
                product.display_name,
                move.location_id.complete_name, move.location_id.id,
                len(all_quants)
            )
            for q in all_quants:
                _logger.info(
                    "WholeLot: [PROP]   Quant ID %d: lot=%s, qty=%.4f, "
                    "reserved=%.4f, available=%.4f, location=%s (ID:%d)",
                    q.id,
                    q.lot_id.name if q.lot_id else 'NONE',
                    q.quantity, q.reserved_quantity,
                    q.quantity - q.reserved_quantity,
                    q.location_id.complete_name, q.location_id.id
                )

            # Also check with _gather
            gathered = Quant._gather(product, move.location_id, strict=False)
            _logger.info(
                "WholeLot: [PROP] _gather (strict=False) at %s: count=%d",
                move.location_id.complete_name, len(gathered)
            )
            for q in gathered:
                _logger.info(
                    "WholeLot: [PROP]   Gathered quant ID %d: lot=%s, qty=%.4f, "
                    "reserved=%.4f, loc=%s (ID:%d)",
                    q.id,
                    q.lot_id.name if q.lot_id else 'NONE',
                    q.quantity, q.reserved_quantity,
                    q.location_id.complete_name, q.location_id.id
                )

            # ---- Reserve each lot ----
            total_reserved = 0.0
            for lot_data in lot_assignments:
                lot = lot_data['lot_id']
                qty = lot_data['qty']

                _logger.info(
                    "WholeLot: [PROP] >> Attempting lot %s (ID:%d), target_qty=%.4f",
                    lot.name, lot.id, qty
                )

                if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                    _logger.info("WholeLot: [PROP] >> SKIP - qty <= 0")
                    continue

                # Check availability for this specific lot
                lot_quants = Quant._gather(
                    product, move.location_id, lot_id=lot, strict=False
                )
                available = sum(q.quantity - q.reserved_quantity for q in lot_quants)

                _logger.info(
                    "WholeLot: [PROP] >> Lot %s: gather found %d quants, "
                    "available=%.4f, details=%s",
                    lot.name, len(lot_quants), available,
                    [(q.id, q.quantity, q.reserved_quantity,
                      q.location_id.complete_name, q.location_id.id)
                     for q in lot_quants]
                )

                if float_compare(available, 0, precision_rounding=rounding) <= 0:
                    _logger.warning(
                        "WholeLot: [PROP] >> Lot %s NOT AVAILABLE (%.4f) at %s",
                        lot.name, available, move.location_id.complete_name
                    )
                    continue

                reserve_qty = min(qty, available)
                _logger.info(
                    "WholeLot: [PROP] >> Calling _update_reserved_quantity "
                    "for lot %s, qty=%.4f",
                    lot.name, reserve_qty
                )

                try:
                    reserved = Quant._update_reserved_quantity(
                        product, move.location_id, reserve_qty,
                        lot_id=lot, strict=False
                    )

                    _logger.info(
                        "WholeLot: [PROP] >> _update_reserved_quantity returned: "
                        "%s (type=%s)",
                        reserved, type(reserved).__name__
                    )

                    if isinstance(reserved, (int, float)):
                        actual_reserved = reserved
                    else:
                        actual_reserved = sum(r[1] for r in reserved) if reserved else 0

                    if float_compare(actual_reserved, 0, precision_rounding=rounding) > 0:
                        self._create_whole_lot_move_line(
                            move, lot, actual_reserved, product
                        )
                        total_reserved += actual_reserved
                        _logger.info(
                            "WholeLot: [PROP] >> ✓ RESERVED lot '%s' (%.4f %s) "
                            "-> picking %s",
                            lot.name, actual_reserved, product.uom_id.name,
                            move.picking_id.name if move.picking_id else 'N/A'
                        )
                    else:
                        _logger.warning(
                            "WholeLot: [PROP] >> _update_reserved_quantity "
                            "returned 0 for lot %s",
                            lot.name
                        )
                except Exception as e:
                    _logger.warning(
                        "WholeLot: [PROP] >> EXCEPTION reserving lot '%s': %s",
                        lot.name, str(e),
                        exc_info=True
                    )
                    continue

            _logger.info(
                "WholeLot: [PROP] ==== RESULT: total_reserved=%.4f / demand=%.4f "
                "for move %d ====",
                total_reserved, total_demand, move.id
            )

            # Update move state
            if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                new_reserved = self._get_reserved_qty(move)
                if float_compare(new_reserved, total_demand,
                                 precision_rounding=rounding) >= 0:
                    move.write({'state': 'assigned'})
                    _logger.info(
                        "WholeLot: [PROP] Move %d -> state=assigned", move.id
                    )
                elif move.state != 'partially_available':
                    move.write({'state': 'partially_available'})
                    _logger.info(
                        "WholeLot: [PROP] Move %d -> state=partially_available",
                        move.id
                    )
            else:
                _logger.warning(
                    "WholeLot: [PROP] ⚠ NOTHING reserved for move %d. "
                    "Quants not at %s (ID:%d) or already fully reserved.",
                    move.id, move.location_id.complete_name, move.location_id.id
                )

        _logger.info("WholeLot: === _assign_whole_lots_from_origin END ===")

    def _create_whole_lot_move_line(self, move, lot, quantity, product):
        """Create a stock.move.line for a whole-lot reservation."""
        rounding = product.uom_id.rounding

        uom_qty = product.uom_id._compute_quantity(
            quantity, move.product_uom, rounding_method='HALF-UP'
        )

        vals = {
            'move_id': move.id,
            'product_id': product.id,
            'product_uom_id': move.product_uom.id,
            'location_id': move.location_id.id,
            'location_dest_id': move.location_dest_id.id,
            'lot_id': lot.id if lot else False,
            'lot_name': lot.name if lot else False,
            'company_id': move.company_id.id or self.env.company.id,
        }

        if 'reserved_uom_qty' in self.env['stock.move.line']._fields:
            vals['reserved_uom_qty'] = uom_qty
        else:
            vals['reserved_uom_qty'] = uom_qty

        if move.picking_id:
            vals['picking_id'] = move.picking_id.id

        quants = self.env['stock.quant']._gather(
            product, move.location_id, lot_id=lot, strict=False
        )
        if quants:
            first_quant = quants[0]
            if first_quant.package_id:
                vals['package_id'] = first_quant.package_id.id
            if first_quant.owner_id:
                vals['owner_id'] = first_quant.owner_id.id

        return self.env['stock.move.line'].create(vals)