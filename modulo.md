## ./__init__.py
```py
# -*- coding: utf-8 -*-
from . import models
```

## ./__manifest__.py
```py
# -*- coding: utf-8 -*-
{
    'name': 'Estrategia de Remoción - Lotes Completos (Sin Selección Automática)',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Estrategia de remoción que solo reserva lotes completos, sin dividir cantidades entre lotes parciales.',
    'description': """
Estrategia de Remoción: Lotes Completos (Sin Selección Automática)
===================================================================

Esta estrategia de remoción está diseñada para productos vendidos por lotes
completos (como placas de piedra, mármol, etc.) donde cada lote representa
una unidad indivisible.

**Comportamiento:**
- Solo reserva lotes cuya cantidad disponible completa puede ser utilizada.
- NUNCA divide un lote para llenar parcialmente la demanda.
- Si la demanda es de 15 m² y hay lotes de 8 m², 6 m² y 10 m²:
  - Selecciona combinaciones de lotes completos que cumplan o se acerquen a la demanda.
  - NO tomará 8 m² de un lote de 10 m².
- Si no hay combinación exacta de lotes completos, reserva los que pueda
  y deja el resto sin reservar para selección manual.
- Los lotes se ordenan por FIFO (fecha de entrada) por defecto.

**Caso de uso principal:**
Empresas de distribución de piedra natural, mármol, granito donde cada
lote/placa es una pieza única e indivisible.
    """,
    'author': 'Alphaqueb Consulting',
    'website': 'https://www.alphaqueb.com',
    'depends': ['stock'],
    'data': [
        'data/product_removal_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
```

## ./data/product_removal_data.xml
```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <!-- Registrar la nueva estrategia de remoción -->
        <record id="removal_whole_lot" model="product.removal">
            <field name="name">Lotes Completos (Sin Selección Automática)</field>
            <field name="method">whole_lot</field>
        </record>
    </data>
</odoo>
```

## ./models/__init__.py
```py
# -*- coding: utf-8 -*-
from . import stock_quant
from . import stock_move
```

## ./models/stock_move.py
```py
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

        This ensures minimal interference with the standard Odoo flow.
        """
        whole_lot_moves = self.env['stock.move']
        regular_moves = self.env['stock.move']

        for move in self:
            if move.state in ('confirmed', 'partially_available', 'waiting') \
                    and move._should_use_whole_lot_strategy():
                whole_lot_moves |= move
            else:
                regular_moves |= move

        # Process regular moves with standard Odoo logic
        if regular_moves:
            super(StockMove, regular_moves)._action_assign(force_qty=force_qty)

        # Process whole-lot moves with our custom logic
        if whole_lot_moves:
            whole_lot_moves._assign_whole_lots()

        return True

    def _get_reserved_qty(self, move):
        """Get the currently reserved quantity for a move in its product UoM.
        Compatible with Odoo 19 where reserved_availability no longer exists.
        """
        # In Odoo 19, we compute reserved qty from move_line_ids
        if hasattr(move, 'forecast_availability'):
            # Odoo 19: use move_line_ids to sum reserved quantities
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
        # Fallback for older Odoo versions
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

            # How much do we still need?
            # In Odoo 19, product_uom_qty is in the move's UoM
            total_demand_move_uom = move.product_uom_qty
            # Convert to product UoM
            total_demand = move.product_uom._compute_quantity(
                total_demand_move_uom, product.uom_id, rounding_method='HALF-UP'
            )

            already_reserved = self._get_reserved_qty(move)
            need = total_demand - already_reserved

            if float_is_zero(need, precision_rounding=rounding) or \
                    float_compare(need, 0, precision_rounding=rounding) <= 0:
                continue

            # Step 1: Get available lots with their full quantities
            available_lots = Quant._get_whole_lot_available_quants(
                product, move.location_id
            )

            if not available_lots:
                _logger.info(
                    "WholeLot: No lots available for %s at %s",
                    product.display_name, move.location_id.complete_name
                )
                continue

            # Step 2: Select which complete lots to reserve
            selected = Quant._whole_lot_select_lots(available_lots, need, rounding)

            if not selected:
                _logger.info(
                    "WholeLot: Cannot select complete lots for demand %.2f %s of %s. "
                    "Available: %s",
                    need, product.uom_id.name, product.display_name,
                    [(d['lot_id'].name, d['available_qty']) for d in available_lots]
                )
                continue

            # Step 3: Reserve each complete lot
            total_reserved = 0.0
            for lot_data in selected:
                lot = lot_data['lot_id']
                qty = lot_data['available_qty']

                if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                    continue

                try:
                    # Update quant reservations
                    reserved = Quant._update_reserved_quantity(
                        product, move.location_id, qty,
                        lot_id=lot, strict=False
                    )

                    if isinstance(reserved, (int, float)):
                        actual_reserved = reserved
                    else:
                        # In some versions, _update_reserved_quantity returns
                        # a list of (quant, qty) tuples
                        actual_reserved = sum(r[1] for r in reserved) if reserved else 0

                    if float_compare(actual_reserved, 0, precision_rounding=rounding) > 0:
                        # Create the stock.move.line
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

            # Step 4: Update move state based on total reserved vs demand
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

    def _create_whole_lot_move_line(self, move, lot, quantity, product):
        """Create a stock.move.line for a whole-lot reservation.

        This method creates the move line that links the reservation to
        the specific lot. It handles UoM conversion and package/owner info.
        """
        rounding = product.uom_id.rounding

        # Convert from product UoM to move UoM
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

        # Set the reserved quantity field (name varies by version)
        # Odoo 18/19 uses 'reserved_uom_qty'
        if 'reserved_uom_qty' in self.env['stock.move.line']._fields:
            vals['reserved_uom_qty'] = uom_qty
        else:
            vals['reserved_uom_qty'] = uom_qty

        # Add picking reference if available
        if move.picking_id:
            vals['picking_id'] = move.picking_id.id

        # Check for package and owner on the quants
        quants = self.env['stock.quant']._gather(
            product, move.location_id, lot_id=lot, strict=False
        )
        if quants:
            first_quant = quants[0]
            if first_quant.package_id:
                vals['package_id'] = first_quant.package_id.id
            if first_quant.owner_id:
                vals['owner_id'] = first_quant.owner_id.id

        return self.env['stock.move.line'].create(vals)```

## ./models/stock_quant.py
```py
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
```

