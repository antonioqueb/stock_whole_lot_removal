# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating a picking, propagate lots to downstream pickings
        that use the whole_lot removal strategy.

        The key insight: during standard validation, _action_assign is called
        on downstream moves BEFORE quants are moved. So our whole_lot strategy
        defers those moves. Here, AFTER validation is complete and quants have
        been moved, we force-assign those deferred moves.
        """
        res = super().button_validate()

        for picking in self:
            if picking.state != 'done':
                continue

            picking._propagate_whole_lots_to_next_step()

        return res

    def _propagate_whole_lots_to_next_step(self):
        """Find downstream moves that were deferred and assign lots to them."""
        self.ensure_one()

        # Find all downstream moves from this picking's moves
        next_moves = self.env['stock.move']
        for move in self.move_ids:
            if move.state == 'done':
                next_moves |= move.move_dest_ids

        if not next_moves:
            return

        # Filter to only whole_lot strategy moves that need reservation
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

        deferred_moves._assign_whole_lots_from_origin()