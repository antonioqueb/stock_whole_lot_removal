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
        """Override to intercept moves that use whole_lot removal strategy.

        We split moves into two groups:
        - whole_lot moves: handled by our custom logic
        - regular moves: handled by Odoo's standard logic

        For whole_lot moves that have origin moves (multi-step), we SKIP
        reservation here because quants may not have arrived yet. Those
        will be handled by _propagate_lots_from_pick() after validation.
        """
        whole_lot_moves = self.env['stock.move']
        whole_lot_deferred = self.env['stock.move']
        regular_moves = self.env['stock.move']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting') \
                    or not move._should_use_whole_lot_strategy():
                regular_moves |= move
                continue

            # Check if this move has origin moves that are being validated
            # (multi-step scenario: PICK → OUT)
            has_pending_origins = any(
                orig.state not in ('done', 'cancel')
                for orig in move.move_orig_ids
            ) if move.move_orig_ids else False

            has_done_origins = any(
                orig.state == 'done'
                for orig in move.move_orig_ids
            ) if move.move_orig_ids else False

            if move.move_orig_ids and (has_pending_origins or has_done_origins):
                # Defer: will be handled after PICK validation
                whole_lot_deferred |= move
                _logger.info(
                    "WholeLot: Deferring reservation for %s (picking %s) - "
                    "has origin moves (pending=%s, done=%s)",
                    move.product_id.display_name,
                    move.picking_id.name if move.picking_id else 'N/A',
                    has_pending_origins, has_done_origins
                )
            else:
                whole_lot_moves |= move

        # Process regular moves with standard Odoo logic
        if regular_moves:
            super(StockMove, regular_moves)._action_assign(force_qty=force_qty)

        # Process whole-lot moves WITHOUT origin (single-step or first step)
        if whole_lot_moves:
            whole_lot_moves._assign_whole_lots()

        # Deferred moves: do nothing now, they'll be assigned after PICK validates

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
        """Reserve stock using whole-lot strategy.

        For each move, we:
        1. Find all available quants grouped by lot
        2. Select only complete lots that fit the demand
        3. Create move lines for each selected lot
        4. Update move state accordingly

        CRITICAL: We never split a lot. Each lot is either fully reserved or not at all.
        """
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

            # Get available lots
            available_lots = Quant._get_whole_lot_available_quants(
                product, move.location_id
            )

            if not available_lots:
                _logger.info(
                    "WholeLot: No lots available for %s at %s",
                    product.display_name, move.location_id.complete_name
                )
                continue

            # Select which complete lots to reserve
            selected = Quant._whole_lot_select_lots(available_lots, need, rounding)

            if not selected:
                _logger.info(
                    "WholeLot: Cannot select complete lots for demand %.2f %s of %s. "
                    "Available: %s",
                    need, product.uom_id.name, product.display_name,
                    [(d['lot_id'].name, d['available_qty']) for d in available_lots]
                )
                continue

            # Reserve each complete lot
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

            # Update move state
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

            # Get lots from origin moves
            lot_assignments = []
            for orig_move in move.move_orig_ids:
                if orig_move.state != 'done':
                    continue
                for ml in orig_move.move_line_ids:
                    if ml.lot_id and float_compare(
                        ml.quantity, 0, precision_rounding=rounding
                    ) > 0:
                        lot_assignments.append({
                            'lot_id': ml.lot_id,
                            'qty': ml.quantity,
                        })

            if not lot_assignments:
                # No origin lots found, fall back to standard whole-lot selection
                _logger.info(
                    "WholeLot: No origin lots for %s, falling back to standard selection",
                    product.display_name
                )
                move._assign_whole_lots()
                continue

            _logger.info(
                "WholeLot: Propagating %d lot(s) from origin to %s (picking %s): %s",
                len(lot_assignments),
                product.display_name,
                move.picking_id.name if move.picking_id else 'N/A',
                [(la['lot_id'].name, la['qty']) for la in lot_assignments]
            )

            total_reserved = 0.0
            for lot_data in lot_assignments:
                lot = lot_data['lot_id']
                qty = lot_data['qty']

                if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                    continue

                # Verify availability at the source location
                quants = Quant._gather(
                    product, move.location_id, lot_id=lot, strict=False
                )
                available = sum(q.quantity - q.reserved_quantity for q in quants)

                if float_compare(available, 0, precision_rounding=rounding) <= 0:
                    _logger.warning(
                        "WholeLot: Lot %s not available at %s (available=%.2f)",
                        lot.name, move.location_id.complete_name, available
                    )
                    continue

                reserve_qty = min(qty, available)

                try:
                    reserved = Quant._update_reserved_quantity(
                        product, move.location_id, reserve_qty,
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
                            "WholeLot: ✓ Propagated lot '%s' (%.2f %s) to picking %s",
                            lot.name, actual_reserved, product.uom_id.name,
                            move.picking_id.name if move.picking_id else 'N/A'
                        )
                except Exception as e:
                    _logger.warning(
                        "WholeLot: Failed to propagate lot '%s': %s",
                        lot.name, str(e)
                    )
                    continue

            # Update move state
            if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                new_reserved = self._get_reserved_qty(move)
                if float_compare(new_reserved, total_demand, precision_rounding=rounding) >= 0:
                    move.write({'state': 'assigned'})
                elif move.state != 'partially_available':
                    move.write({'state': 'partially_available'})

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