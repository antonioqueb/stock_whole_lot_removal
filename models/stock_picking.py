# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating a picking, propagate lots to downstream pickings
        that use the whole_lot removal strategy.

        During standard validation, _action_assign on downstream moves is
        deferred (our whole_lot strategy skips moves with move_orig_ids).
        After validation completes and quants have moved to the intermediate
        location, we call standard _action_assign WITH skip_whole_lot_strategy
        context so Odoo's native reservation picks up those quants.
        """
        res = super().button_validate()

        for picking in self:
            if picking.state != 'done':
                continue
            picking._propagate_whole_lots_to_next_step()

        return res

    def _propagate_whole_lots_to_next_step(self):
        """Find downstream moves that were deferred and assign them using
        standard Odoo logic (bypassing whole_lot strategy).

        Since the PICK already moved the correct lots to the intermediate
        location, standard Odoo reservation will find those quants and
        reserve them. No lot splitting happens because each quant IS
        a complete lot.
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

        # Call standard _action_assign bypassing our whole_lot override
        deferred_moves.with_context(skip_whole_lot_strategy=True)._action_assign()

        # Log results
        for move in deferred_moves:
            _logger.info(
                "WholeLot: Post-propagation result: move %d state=%s, "
                "move_lines=%d, lots=%s (picking %s)",
                move.id, move.state,
                len(move.move_line_ids),
                [(ml.lot_id.name, ml.reserved_uom_qty)
                 for ml in move.move_line_ids if ml.lot_id],
                move.picking_id.name if move.picking_id else 'N/A'
            )