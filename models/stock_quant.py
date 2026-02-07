# -*- coding: utf-8 -*-
import logging
from odoo import models, api
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def _get_removal_strategy_order(self, removal_strategy):
        """Extend to handle 'whole_lot' removal strategy.
        For whole_lot, we use FIFO ordering but the actual filtering
        of whole lots happens in _update_reserved_quantity.
        """
        if removal_strategy == 'whole_lot':
            # Order by incoming date (FIFO) - the actual whole-lot logic
            # is handled in _gather and _update_reserved_quantity
            return 'in_date ASC NULLS FIRST, id ASC'
        return super()._get_removal_strategy_order(removal_strategy)

    @api.model
    def _get_removal_strategy(self, product_id, location_id):
        """Get the removal strategy for a product/location combo."""
        result = super()._get_removal_strategy(product_id, location_id)
        return result

    def _get_whole_lot_available_quants(self, product_id, location_id, lot_id=None,
                                         package_id=None, owner_id=None, strict=False):
        """Get all quants grouped by lot with their available quantities.
        Returns a list of tuples: (lot_id, available_qty, quants_recordset)
        sorted by in_date ASC (FIFO).
        Only includes lots where available_qty > 0.
        """
        # Gather all quants for the product/location
        quants = self._gather(product_id, location_id, lot_id=lot_id,
                              package_id=package_id, owner_id=owner_id, strict=strict)

        lot_data = {}
        rounding = product_id.uom_id.rounding

        for quant in quants:
            lot_key = quant.lot_id.id or False
            if lot_key not in lot_data:
                lot_data[lot_key] = {
                    'lot_id': quant.lot_id,
                    'available_qty': 0.0,
                    'quants': self.env['stock.quant'],
                    'in_date': quant.in_date,
                }
            lot_data[lot_key]['available_qty'] += quant.quantity - quant.reserved_quantity
            lot_data[lot_key]['quants'] |= quant

        # Filter lots with available quantity > 0 and sort by date (FIFO)
        available_lots = []
        for lot_key, data in lot_data.items():
            if float_compare(data['available_qty'], 0, precision_rounding=rounding) > 0:
                available_lots.append(data)

        available_lots.sort(key=lambda x: (x['in_date'] or ''))
        return available_lots

    @api.model
    def _whole_lot_select_lots(self, available_lots, need, rounding):
        """Select which complete lots to reserve to fulfill the demand.

        Strategy:
        1. Try to find an exact combination of whole lots that matches the demand.
        2. If no exact match, greedily select lots (FIFO) where each lot's
           FULL available quantity is <= remaining need.
        3. NEVER split a lot - each lot is taken completely or not at all.

        Args:
            available_lots: list of dicts with 'lot_id', 'available_qty', 'quants', 'in_date'
            need: quantity demanded
            rounding: UoM rounding

        Returns:
            list of selected lot dicts
        """
        if not available_lots:
            return []

        # Step 1: Try exact match with a single lot first
        for lot_data in available_lots:
            if float_compare(lot_data['available_qty'], need, precision_rounding=rounding) == 0:
                return [lot_data]

        # Step 2: Try to find the best combination using a greedy approach
        # Select lots (FIFO order) whose full qty fits within remaining need
        selected = []
        remaining_need = need

        for lot_data in available_lots:
            lot_qty = lot_data['available_qty']
            cmp = float_compare(lot_qty, remaining_need, precision_rounding=rounding)

            if cmp <= 0:
                # This lot's full quantity fits within (or equals) remaining need
                selected.append(lot_data)
                remaining_need -= lot_qty

                if float_is_zero(remaining_need, precision_rounding=rounding):
                    break
            # If lot_qty > remaining_need, skip it (don't split)

        # Step 3: If greedy didn't satisfy demand fully, try to find a better combo
        # using a secondary pass - look for a single lot that could fill remaining need
        if float_compare(remaining_need, 0, precision_rounding=rounding) > 0:
            # Check if any remaining lot can exactly fill the gap
            selected_lot_ids = {d['lot_id'].id for d in selected}
            for lot_data in available_lots:
                if lot_data['lot_id'].id in selected_lot_ids:
                    continue
                if float_compare(lot_data['available_qty'], remaining_need,
                                 precision_rounding=rounding) == 0:
                    selected.append(lot_data)
                    remaining_need = 0
                    break

        return selected
