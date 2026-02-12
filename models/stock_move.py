# -*- coding: utf-8 -*-
import logging
import json
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
        # Allow bypassing our strategy via context
        if self.env.context.get('skip_whole_lot_strategy'):
            return super()._action_assign(force_qty=force_qty)

        whole_lot_moves = self.env['stock.move']
        whole_lot_deferred = self.env['stock.move']
        regular_moves = self.env['stock.move']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting') \
                    or not move._should_use_whole_lot_strategy():
                regular_moves |= move
                continue

            # Si tiene origen (es el paso 2 o 3 de una cadena), a veces queremos diferirlo
            if move.move_orig_ids and not self.env.context.get('force_whole_lot_assign'):
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

        # Process whole-lot moves
        if whole_lot_moves:
            _logger.info(f"WholeLot: Processing {len(whole_lot_moves)} moves with Whole Lot Strategy...")
            whole_lot_moves._assign_whole_lots()

        return True

    def _get_reserved_qty(self, move):
        """Get the currently reserved quantity for a move in its product UoM."""
        reserved = 0.0
        for ml in move.move_line_ids:
            ml_qty = ml.quantity if 'quantity' in ml._fields else 0.0
            reserved += ml.product_uom_id._compute_quantity(
                ml_qty, move.product_id.uom_id,
                rounding_method='HALF-UP'
            )
        return reserved

    def _get_already_delivered_lot_ids(self, sale_line):
        """
        Obtiene los IDs de lotes que ya fueron entregados (en moves done)
        para una línea de venta específica.
        
        Esto es CRÍTICO para backorders: cuando entregas parcialmente,
        los lotes ya entregados no deben ser buscados de nuevo en inventario.
        """
        delivered_lot_ids = set()
        
        if not sale_line:
            return delivered_lot_ids
        
        # Buscar todos los moves DONE de esta SO line
        done_moves = sale_line.move_ids.filtered(lambda m: m.state == 'done')
        
        for move in done_moves:
            for ml in move.move_line_ids:
                if ml.lot_id:
                    delivered_lot_ids.add(ml.lot_id.id)
        
        if delivered_lot_ids:
            _logger.info(
                "WholeLot: Found %d already-delivered lots for SO Line %s: %s",
                len(delivered_lot_ids), sale_line.id, list(delivered_lot_ids)
            )
        
        return delivered_lot_ids

    def _assign_whole_lots(self):
        """Reserve stock using whole-lot strategy with Dynamic Module Linking and Recovery."""
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

            _logger.info("=" * 80)
            _logger.info(f"WholeLot: Move {move.id} [{product.default_code}] @ {move.location_id.display_name}")
            _logger.info(f"WholeLot: Demand: {total_demand:.2f}, Reserved: {already_reserved:.2f}, Need: {need:.2f}")

            if float_is_zero(need, precision_rounding=rounding) or \
                    float_compare(need, 0, precision_rounding=rounding) <= 0:
                _logger.info("WholeLot: Need is zero, skipping.")
                continue

            # 1. Obtener TODOS los lotes físicamente disponibles en la ubicación
            available_lots = Quant._get_whole_lot_available_quants(
                product, move.location_id
            )

            # LOG DE DIAGNÓSTICO
            avail_debug = [f"{d['lot_id'].name} ({d['available_qty']:.2f})" for d in available_lots]
            _logger.info(f"WholeLot: Physical Availability at {move.location_id.name} (Count: {len(available_lots)}): {avail_debug}")

            if not available_lots:
                _logger.warning(
                    "WholeLot: STOP - No physical lots available for %s at %s",
                    product.display_name, move.location_id.complete_name
                )
                continue

            # ==============================================================================
            # VINCULACIÓN DINÁMICA: RECUPERACIÓN DE LOTES (SOLUCIÓN BACKORDER)
            # ==============================================================================
            allowed_lot_ids = set()
            has_restriction = False
            
            # ── NUEVO: Obtener lotes ya entregados para excluirlos ──
            already_delivered_ids = set()
            if move.sale_line_id:
                already_delivered_ids = self._get_already_delivered_lot_ids(move.sale_line_id)
            
            if move.sale_line_id:
                sol = move.sale_line_id
                
                # FUENTE 1: JSON Breakdown (Más confiable para backorders, no se suele borrar)
                if hasattr(sol, 'x_lot_breakdown_json') and sol.x_lot_breakdown_json:
                    try:
                        json_data = sol.x_lot_breakdown_json
                        if isinstance(json_data, str):
                            json_data = json.loads(json_data)
                        
                        if isinstance(json_data, dict):
                            json_ids = [int(k) for k in json_data.keys() if k.isdigit()]
                            if json_ids:
                                has_restriction = True
                                allowed_lot_ids.update(json_ids)
                                _logger.info(f"WholeLot: Recovered {len(json_ids)} lots from SO.x_lot_breakdown_json")
                    except Exception as e:
                        _logger.warning(f"WholeLot: Failed to parse x_lot_breakdown_json: {e}")

                # FUENTE 2: x_selected_lots (Carrito original, Backup confiable)
                if hasattr(sol, 'x_selected_lots') and sol.x_selected_lots:
                    cart_lot_ids = sol.x_selected_lots.mapped('lot_id').ids
                    if cart_lot_ids:
                        has_restriction = True
                        prev_len = len(allowed_lot_ids)
                        allowed_lot_ids.update(cart_lot_ids)
                        diff = len(allowed_lot_ids) - prev_len
                        if diff > 0:
                            _logger.info(f"WholeLot: Recovered {diff} additional lots from SO.x_selected_lots")

                # FUENTE 3: lot_ids (Selección actual, puede estar parcial/incompleta en backorders)
                if hasattr(sol, 'lot_ids') and sol.lot_ids:
                    has_restriction = True
                    stone_ids = sol.lot_ids.ids
                    prev_len = len(allowed_lot_ids)
                    allowed_lot_ids.update(stone_ids)
                    diff = len(allowed_lot_ids) - prev_len
                    if diff > 0:
                        _logger.info(f"WholeLot: Found {diff} additional lots in SO.lot_ids")

            if has_restriction:
                # ── NUEVO: Excluir lotes ya entregados ──
                if already_delivered_ids:
                    before_count = len(allowed_lot_ids)
                    allowed_lot_ids -= already_delivered_ids
                    excluded = before_count - len(allowed_lot_ids)
                    if excluded > 0:
                        _logger.info(
                            "WholeLot: Excluded %d already-delivered lots. "
                            "Remaining target: %s", excluded, list(allowed_lot_ids)
                        )
                
                _logger.info(f"WholeLot: [RESTRICTION APPLIED] Combined Target Lot IDs: {list(allowed_lot_ids)}")
                
                # Filtrar available_lots para dejar SOLO los que están permitidos
                filtered_lots = []
                for lot_data in available_lots:
                    if lot_data['lot_id'].id in allowed_lot_ids:
                        filtered_lots.append(lot_data)
                
                original_count = len(available_lots)
                available_lots = filtered_lots
                
                _logger.info(
                    f"WholeLot: Filter result: {original_count} -> {len(available_lots)} candidates. "
                    f"Valid candidates: {[d['lot_id'].name for d in available_lots]}"
                )
                
                if not available_lots:
                    _logger.error(
                        "WholeLot: CRITICAL - The SO requires specific lots (checked JSON/Cart/Line), "
                        "but NONE of them are physically in the source location. "
                        "Preventing random assignment."
                    )
                    continue
            else:
                _logger.info("WholeLot: No SO restrictions detected (Open selection).")
            # ==============================================================================

            # 2. Selección Matemática (Algoritmo de optimización)
            selected = Quant._whole_lot_select_lots(available_lots, need, rounding)

            if not selected:
                _logger.info(
                    "WholeLot: Math logic could not select complete lots for demand %.2f. Candidates: %s",
                    need, [d['lot_id'].name for d in available_lots]
                )
                continue

            # 3. Ejecutar Reserva
            total_reserved = 0.0
            for lot_data in selected:
                lot = lot_data['lot_id']
                qty = lot_data['available_qty']

                if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                    continue

                try:
                    reserved_before = sum(
                        q.reserved_quantity
                        for q in Quant._gather(
                            product, move.location_id, lot_id=lot, strict=False
                        )
                    )

                    Quant._update_reserved_quantity(
                        product, move.location_id, qty,
                        lot_id=lot, strict=False
                    )

                    reserved_after = sum(
                        q.reserved_quantity
                        for q in Quant._gather(
                            product, move.location_id, lot_id=lot, strict=False
                        )
                    )

                    actual_reserved = reserved_after - reserved_before

                    if float_compare(actual_reserved, 0, precision_rounding=rounding) > 0:
                        self._create_whole_lot_move_line(
                            move, lot, actual_reserved, product
                        )
                        total_reserved += actual_reserved
                        _logger.info(
                            "WholeLot: SUCCESS - Reserved lot '%s' (%.2f %s)",
                            lot.name, actual_reserved, product.uom_id.name
                        )
                    else:
                        _logger.warning(
                            "WholeLot: FAILED - Reservation had no effect for lot '%s'. "
                            "Maybe reserved by another user?", lot.name
                        )
                except Exception as e:
                    _logger.error(
                        "WholeLot: EXCEPTION reserving lot '%s': %s",
                        lot.name if lot else 'N/A', str(e)
                    )
                    continue

            # 4. Actualizar Estado del Movimiento
            if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                new_reserved = self._get_reserved_qty(move)
                cmp = float_compare(
                    new_reserved, total_demand,
                    precision_rounding=rounding
                )
                if cmp >= 0:
                    move.write({'state': 'assigned'})
                    _logger.info("WholeLot: Move state updated to ASSIGNED")
                elif move.state != 'partially_available':
                    move.write({'state': 'partially_available'})
                    _logger.info("WholeLot: Move state updated to PARTIALLY AVAILABLE")
            
            _logger.info("=" * 80)

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

        # In Odoo 19, stock.move.line uses 'quantity' for reserved qty
        if 'reserved_uom_qty' in self.env['stock.move.line']._fields:
            vals['reserved_uom_qty'] = uom_qty
        else:
            vals['quantity'] = uom_qty

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