# -*- coding: utf-8 -*-
import logging
from odoo import models, api
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model
    def _get_removal_strategy_order(self, removal_strategy):
        """Extend to handle 'whole_lot' and 'whole_lot_partial' removal strategies."""
        if removal_strategy in ('whole_lot', 'whole_lot_partial'):
            return 'in_date ASC NULLS FIRST, id ASC'
        return super()._get_removal_strategy_order(removal_strategy)

    @api.model
    def _get_removal_strategy(self, product_id, location_id):
        result = super()._get_removal_strategy(product_id, location_id)
        return result

    def _get_whole_lot_available_quants(self, product_id, location_id, lot_id=None,
                                         package_id=None, owner_id=None, strict=False):
        """Get all quants grouped by lot with their available quantities (FIFO)."""
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

        available_lots = []
        for lot_key, data in lot_data.items():
            if float_compare(data['available_qty'], 0, precision_rounding=rounding) > 0:
                available_lots.append(data)

        available_lots.sort(key=lambda x: (x['in_date'] or ''))
        return available_lots

    @api.model
    def _whole_lot_select_lots(self, available_lots, need, rounding):
        """Select which complete lots to reserve to fulfill the demand ('whole_lot' method only)."""
        if not available_lots:
            return []

        for lot_data in available_lots:
            if float_compare(lot_data['available_qty'], need, precision_rounding=rounding) == 0:
                return [lot_data]

        selected = []
        remaining_need = need

        for lot_data in available_lots:
            lot_qty = lot_data['available_qty']
            cmp = float_compare(lot_qty, remaining_need, precision_rounding=rounding)
            if cmp <= 0:
                selected.append(lot_data)
                remaining_need -= lot_qty
                if float_is_zero(remaining_need, precision_rounding=rounding):
                    break

        if float_compare(remaining_need, 0, precision_rounding=rounding) > 0:
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
