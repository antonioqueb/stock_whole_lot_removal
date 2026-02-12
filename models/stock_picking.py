# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating a picking, propagate lots to:
        1. Downstream moves (OUT step) that were deferred
        2. Backorder PICK moves that need correct lot assignment

        During standard validation, _action_assign on downstream moves is
        deferred by our whole_lot strategy (skips moves with move_orig_ids).
        For backorders, Odoo creates them but our strategy's _assign_whole_lots
        couldn't find the lots because they were still reserved by the original
        PICK. After validation completes and quants are freed, we can now
        properly assign them.
        """
        # Collect backorder picking IDs that will be created
        # (pickings that exist before validation with backorder_id = self)
        # Actually, backorders are created DURING super().button_validate()
        # so we need to detect them after.

        # Remember which pickings we're validating
        validating_ids = self.ids

        res = super().button_validate()

        for picking in self.browse(validating_ids):
            if picking.state != 'done':
                continue

            # 1. Propagar a moves downstream (OUT)
            picking._propagate_whole_lots_to_next_step()

            # 2. Asignar lotes correctos al backorder PICK
            picking._assign_whole_lots_to_backorder()

        return res

    def _propagate_whole_lots_to_next_step(self):
        """Find downstream moves that were deferred and assign them.
        
        Since the PICK already moved the correct lots to the intermediate
        location, standard Odoo reservation will find those quants.
        """
        self.ensure_one()

        # Collect downstream moves from done moves
        next_moves = self.env['stock.move']
        for move in self.move_ids:
            if move.state == 'done':
                next_moves |= move.move_dest_ids

        if not next_moves:
            return

        # Filter to whole_lot strategy moves that need reservation
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

        # Use standard Odoo reservation for downstream moves
        # The correct lots are already in the intermediate location
        deferred_moves.with_context(skip_whole_lot_strategy=True)._action_assign()

        # Log results
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
        """Find backorder picking created from this validation and assign
        the correct lots using our whole_lot strategy.

        When a PICK is partially validated:
        1. Odoo creates a backorder PICK with the remaining demand
        2. Odoo calls _action_assign on the backorder
        3. Our _assign_whole_lots runs but can't find the pending lots
           because they were still reserved by the original PICK
        4. NOW (after validation is complete), the quants are freed
        5. We can properly assign the correct pending lots

        This method:
        - Finds the backorder picking
        - Clears any incorrect reservations Odoo may have made
        - Runs _assign_whole_lots with the correct lot restrictions
        """
        self.ensure_one()

        # Find backorder pickings that reference this picking as their origin
        backorder_pickings = self.env['stock.picking'].search([
            ('backorder_id', '=', self.id),
            ('state', 'in', ('confirmed', 'waiting', 'assigned')),
        ])

        if not backorder_pickings:
            return

        for bo_picking in backorder_pickings:
            # Find moves that use whole_lot strategy
            wl_moves = self.env['stock.move']
            for move in bo_picking.move_ids:
                if move.state in ('confirmed', 'partially_available', 'waiting', 'assigned'):
                    try:
                        if move._should_use_whole_lot_strategy():
                            wl_moves |= move
                    except Exception:
                        continue

            if not wl_moves:
                continue

            _logger.info(
                "WholeLot: Post-validation backorder assignment for %s "
                "(backorder of %s) -> %d move(s)",
                bo_picking.name, self.name, len(wl_moves)
            )

            # For each move, clear any incorrect reservations and re-assign
            Quant = self.env['stock.quant']
            for move in wl_moves:
                product = move.product_id
                rounding = product.uom_id.rounding

                # Clear existing move lines (wrong lots from Odoo standard)
                if move.move_line_ids:
                    _logger.info(
                        "WholeLot: Clearing %d incorrect move_lines from backorder "
                        "move %d (lots: %s)",
                        len(move.move_line_ids), move.id,
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
                            except Exception as e:
                                _logger.warning(
                                    "WholeLot: Could not unreserve lot %s: %s",
                                    ml.lot_id.name, e
                                )
                    move.move_line_ids.unlink()
                    move.write({'state': 'confirmed'})

            # Now run our whole_lot strategy - quants are now free
            wl_moves._assign_whole_lots()

            # Log final state
            for move in wl_moves:
                _logger.info(
                    "WholeLot: Backorder move %d final state=%s, "
                    "move_lines=%d, lots=%s (picking %s)",
                    move.id, move.state,
                    len(move.move_line_ids),
                    [(ml.lot_id.name, ml.quantity)
                     for ml in move.move_line_ids if ml.lot_id],
                    bo_picking.name
                )