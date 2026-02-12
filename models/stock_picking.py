# -*- coding: utf-8 -*-
import logging
from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        """After validating a picking, propagate lots to downstream pickings
        that use the whole_lot removal strategy.
        """
        _logger.info(
            "WholeLot: [PICK] button_validate ENTER for %s",
            ', '.join(self.mapped('name'))
        )

        res = super().button_validate()

        _logger.info(
            "WholeLot: [PICK] button_validate AFTER super() - states: %s",
            [(p.name, p.state) for p in self]
        )

        for picking in self:
            if picking.state != 'done':
                _logger.info(
                    "WholeLot: [PICK] %s state=%s, skipping propagation",
                    picking.name, picking.state
                )
                continue

            picking._propagate_whole_lots_to_next_step()

        return res

    def _propagate_whole_lots_to_next_step(self):
        """Find downstream moves that were deferred and assign lots to them."""
        self.ensure_one()

        _logger.info(
            "WholeLot: [PICK] _propagate_whole_lots_to_next_step for %s",
            self.name
        )

        # Find all downstream moves from this picking's moves
        next_moves = self.env['stock.move']
        for move in self.move_ids:
            _logger.info(
                "WholeLot: [PICK] Checking move ID %d (state=%s): "
                "move_dest_ids=%s",
                move.id, move.state, move.move_dest_ids.ids
            )
            if move.state == 'done':
                next_moves |= move.move_dest_ids

        if not next_moves:
            _logger.info("WholeLot: [PICK] No downstream moves found")
            return

        _logger.info(
            "WholeLot: [PICK] Found %d downstream move(s): %s",
            len(next_moves),
            [(m.id, m.state, m.picking_id.name if m.picking_id else 'N/A')
             for m in next_moves]
        )

        # Filter to only whole_lot strategy moves that need reservation
        deferred_moves = self.env['stock.move']
        for move in next_moves:
            _logger.info(
                "WholeLot: [PICK] Evaluating downstream move ID %d: "
                "state=%s, product=%s, picking=%s",
                move.id, move.state,
                move.product_id.display_name,
                move.picking_id.name if move.picking_id else 'N/A'
            )

            if move.state not in ('confirmed', 'partially_available', 'waiting'):
                _logger.info(
                    "WholeLot: [PICK] SKIP move %d - state '%s' not actionable",
                    move.id, move.state
                )
                continue

            try:
                is_whole_lot = move._should_use_whole_lot_strategy()
                _logger.info(
                    "WholeLot: [PICK] Move %d _should_use_whole_lot_strategy=%s "
                    "(categ=%s, categ_removal=%s)",
                    move.id, is_whole_lot,
                    move.product_id.categ_id.name,
                    move.product_id.categ_id.removal_strategy_id.method
                    if move.product_id.categ_id.removal_strategy_id else 'None'
                )
                if is_whole_lot:
                    deferred_moves |= move
            except Exception as e:
                _logger.warning(
                    "WholeLot: [PICK] Error checking strategy for move %d: %s",
                    move.id, str(e)
                )
                continue

        if not deferred_moves:
            _logger.info(
                "WholeLot: [PICK] No deferred whole_lot moves found among "
                "downstream moves"
            )
            return

        _logger.info(
            "WholeLot: [PICK] Post-validation propagation from %s -> "
            "%d deferred move(s): %s",
            self.name, len(deferred_moves),
            [(m.id, m.product_id.display_name) for m in deferred_moves]
        )

        deferred_moves._assign_whole_lots_from_origin()