# -*- coding: utf-8 -*-
import logging
import json
from odoo import models, api, _
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = 'stock.move'

    # ═══════════════════════════════════════════════════════════════════════════
    # DETECCIÓN DE ESTRATEGIA
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_whole_lot_strategy_type(self):
        """Returns: False, 'whole_lot', or 'whole_lot_partial'."""
        self.ensure_one()
        product = self.product_id
        if product.tracking not in ('lot', 'serial'):
            return False

        if product.categ_id.removal_strategy_id:
            method = product.categ_id.removal_strategy_id.method
            if method in ('whole_lot', 'whole_lot_partial'):
                return method

        location = self.location_id
        while location:
            if location.removal_strategy_id:
                method = location.removal_strategy_id.method
                if method in ('whole_lot', 'whole_lot_partial'):
                    return method
            location = location.location_id
        return False

    def _should_use_whole_lot_strategy(self):
        """Returns True if ANY of the whole_lot strategies applies."""
        return bool(self._get_whole_lot_strategy_type())

    # ═══════════════════════════════════════════════════════════════════════════
    # HOOK _action_assign
    # ═══════════════════════════════════════════════════════════════════════════

    def _action_assign(self, force_qty=False):
        """Override to intercept moves that use whole_lot* removal strategies."""
        # Contexto usado por integraciones que crean reservas exactas manualmente.
        # En ese caso no debe entrar ni a WholeLot ni a la reserva estándar:
        # la integración crea las move lines precisas después.
        if self.env.context.get('skip_whole_lot_no_assign'):
            _logger.info(
                "WholeLot: SKIP assignment by context skip_whole_lot_no_assign. Moves=%s",
                self.ids,
            )
            return True

        if self.env.context.get('skip_whole_lot_strategy'):
            return super()._action_assign(force_qty=force_qty)

        whole_lot_moves = self.env['stock.move']
        regular_moves = self.env['stock.move']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting') \
                    or not move._should_use_whole_lot_strategy():
                regular_moves |= move
                continue

            sol = move.sale_line_id
            if sol and not self._sol_has_manual_lot_selection(sol):
                _logger.info(
                    "WholeLot: SKIP - Move %d (SO Line %d) has no manual lot selection. "
                    "Leaving unreserved for manual picking.",
                    move.id, sol.id
                )
                continue

            if move.move_orig_ids and not self.env.context.get('force_whole_lot_assign'):
                _logger.info(
                    "WholeLot: Deferring reservation for %s (picking %s) - "
                    "has %d origin move(s), states: %s",
                    move.product_id.display_name,
                    move.picking_id.name if move.picking_id else 'N/A',
                    len(move.move_orig_ids),
                    [m.state for m in move.move_orig_ids]
                )
                continue

            whole_lot_moves |= move

        if regular_moves:
            super(StockMove, regular_moves)._action_assign(force_qty=force_qty)

        if whole_lot_moves:
            _logger.info(
                "WholeLot: Processing %s moves with WholeLot strategy...",
                len(whole_lot_moves),
            )
            whole_lot_moves._assign_whole_lots()

        return True

    # ═══════════════════════════════════════════════════════════════════════════
    # HELPERS COMPARTIDOS
    # ═══════════════════════════════════════════════════════════════════════════

    def _sol_has_manual_lot_selection(self, sol):
        """Verifica si una línea de venta tiene lotes seleccionados manualmente."""
        if hasattr(sol, 'lot_ids') and sol.lot_ids:
            return True
        if hasattr(sol, 'x_selected_lots') and sol.x_selected_lots:
            return True
        if hasattr(sol, 'x_lot_breakdown_json') and sol.x_lot_breakdown_json:
            try:
                json_data = sol.x_lot_breakdown_json
                if isinstance(json_data, str):
                    json_data = json.loads(json_data)
                if isinstance(json_data, dict) and json_data:
                    return True
            except Exception:
                pass
        return False

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
        """Obtiene IDs de lotes ya entregados (moves done) para una SO line."""
        delivered_lot_ids = set()
        if not sale_line:
            return delivered_lot_ids
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

    def _get_currently_reserved_lot_ids(self, sale_line, exclude_move=None):
        """Obtiene IDs de lotes reservados en moves activos de la misma SO line."""
        reserved_lot_ids = set()
        if not sale_line:
            return reserved_lot_ids
        active_moves = sale_line.move_ids.filtered(
            lambda m: m.state in ('assigned', 'partially_available') and m != exclude_move
        )
        for move in active_moves:
            for ml in move.move_line_ids:
                if ml.lot_id:
                    reserved_lot_ids.add(ml.lot_id.id)
        if reserved_lot_ids:
            _logger.info(
                "WholeLot: Found %d lots reserved in sibling moves for SO Line %s: %s",
                len(reserved_lot_ids), sale_line.id, list(reserved_lot_ids)
            )
        return reserved_lot_ids

    def _get_sol_lot_selection(self, sol):
        """Extrae la selección de lotes desde TODAS las fuentes posibles.

        Returns:
            dict {
                'lot_ids': set(ids),
                'breakdown': dict {lot_id: qty} — solo lotes con cantidad explícita
            }
        """
        result = {'lot_ids': set(), 'breakdown': {}}
        if not sol:
            return result

        # FUENTE 1: x_lot_breakdown_json (ÚNICA con cantidades explícitas)
        if hasattr(sol, 'x_lot_breakdown_json') and sol.x_lot_breakdown_json:
            try:
                json_data = sol.x_lot_breakdown_json
                if isinstance(json_data, str):
                    json_data = json.loads(json_data)
                if isinstance(json_data, dict):
                    for k, v in json_data.items():
                        if str(k).isdigit():
                            lot_id = int(k)
                            result['lot_ids'].add(lot_id)
                            try:
                                result['breakdown'][lot_id] = float(v)
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                _logger.warning(f"WholeLot: Failed to parse x_lot_breakdown_json: {e}")

        # FUENTE 2: x_selected_lots
        if hasattr(sol, 'x_selected_lots') and sol.x_selected_lots:
            try:
                cart_lot_ids = sol.x_selected_lots.mapped('lot_id').ids
                result['lot_ids'].update(cart_lot_ids)
            except Exception:
                pass

        # FUENTE 3: lot_ids
        if hasattr(sol, 'lot_ids') and sol.lot_ids:
            result['lot_ids'].update(sol.lot_ids.ids)

        return result

    # ═══════════════════════════════════════════════════════════════════════════
    # DISPATCHER PRINCIPAL
    # ═══════════════════════════════════════════════════════════════════════════

    def _assign_whole_lots(self):
        """Dispatch: bifurca entre whole_lot (lotes completos) y whole_lot_partial."""
        Quant = self.env['stock.quant']

        for move in self:
            if move.state not in ('confirmed', 'partially_available', 'waiting'):
                continue

            strategy = move._get_whole_lot_strategy_type()
            if not strategy:
                continue

            product = move.product_id
            rounding = product.uom_id.rounding

            total_demand_move_uom = move.product_uom_qty
            total_demand = move.product_uom._compute_quantity(
                total_demand_move_uom, product.uom_id, rounding_method='HALF-UP'
            )

            already_reserved = self._get_reserved_qty(move)
            need = total_demand - already_reserved

            is_backorder = bool(move.picking_id and move.picking_id.backorder_id)

            _logger.info("=" * 80)
            _logger.info(
                "WholeLot[%s]: Move %d [%s] @ %s%s",
                strategy, move.id, product.default_code, move.location_id.display_name,
                ' [BACKORDER of %s]' % move.picking_id.backorder_id.name if is_backorder else ''
            )
            _logger.info(f"WholeLot: Demand: {total_demand:.2f}, Reserved: {already_reserved:.2f}, Need: {need:.2f}")

            if float_is_zero(need, precision_rounding=rounding) or \
                    float_compare(need, 0, precision_rounding=rounding) <= 0:
                _logger.info("WholeLot: Need is zero, skipping.")
                continue

            available_lots = Quant._get_whole_lot_available_quants(
                product, move.location_id
            )

            avail_debug = [f"{d['lot_id'].name} ({d['available_qty']:.2f})" for d in available_lots]
            _logger.info(f"WholeLot: Physical Availability (Count: {len(available_lots)}): {avail_debug}")

            if not available_lots:
                _logger.warning(
                    "WholeLot: STOP - No physical lots available for %s at %s",
                    product.display_name, move.location_id.complete_name
                )
                continue

            sol = move.sale_line_id
            selection = self._get_sol_lot_selection(sol)
            already_delivered_ids = self._get_already_delivered_lot_ids(sol) if sol else set()
            currently_reserved_ids = self._get_currently_reserved_lot_ids(sol, exclude_move=move) if sol else set()

            allowed_lot_ids = set(selection['lot_ids'])
            has_restriction = bool(allowed_lot_ids)

            if has_restriction:
                if already_delivered_ids:
                    allowed_lot_ids -= already_delivered_ids
                if currently_reserved_ids:
                    allowed_lot_ids -= currently_reserved_ids

                _logger.info(f"WholeLot: [RESTRICTION] Target Lot IDs after exclusions: {list(allowed_lot_ids)}")

                available_lots = [d for d in available_lots if d['lot_id'].id in allowed_lot_ids]

                if not available_lots:
                    _logger.error(
                        "WholeLot: CRITICAL - SO requires specific lots but NONE are physically "
                        "at the source location. Preventing random assignment."
                    )
                    continue
            else:
                if sol:
                    _logger.info(
                        "WholeLot: No SO restrictions for SO Line %s. SKIPPING auto-assignment.",
                        sol.id
                    )
                    continue
                else:
                    _logger.info("WholeLot: No SO restrictions (non-sale move). Open selection.")

            # BIFURCACIÓN
            if strategy == 'whole_lot_partial':
                total_reserved = self._reserve_whole_lot_partial(
                    move, available_lots, selection['breakdown'], rounding
                )
            else:
                total_reserved = self._reserve_whole_lot_complete(
                    move, available_lots, need, rounding
                )

            if float_compare(total_reserved, 0, precision_rounding=rounding) > 0:
                new_reserved = self._get_reserved_qty(move)
                cmp = float_compare(new_reserved, total_demand, precision_rounding=rounding)
                if cmp >= 0:
                    move.write({'state': 'assigned'})
                    _logger.info("WholeLot: Move state updated to ASSIGNED")
                elif move.state != 'partially_available':
                    move.write({'state': 'partially_available'})
                    _logger.info("WholeLot: Move state updated to PARTIALLY AVAILABLE")

            _logger.info("=" * 80)

    # ═══════════════════════════════════════════════════════════════════════════
    # ESTRATEGIA 1: whole_lot (PLACAS)
    # ═══════════════════════════════════════════════════════════════════════════

    def _reserve_whole_lot_complete(self, move, available_lots, need, rounding):
        """Reserva lotes COMPLETOS (sin dividir). Usado por placas."""
        Quant = self.env['stock.quant']
        product = move.product_id

        selected = Quant._whole_lot_select_lots(available_lots, need, rounding)
        if not selected:
            _logger.info(
                "WholeLot[complete]: Could not select complete lots for demand %.2f. Candidates: %s",
                need, [d['lot_id'].name for d in available_lots]
            )
            return 0.0

        total_reserved = 0.0
        for lot_data in selected:
            lot = lot_data['lot_id']
            qty = lot_data['available_qty']
            if float_compare(qty, 0, precision_rounding=rounding) <= 0:
                continue
            reserved = self._do_reserve_lot(move, lot, qty, product, rounding)
            total_reserved += reserved
        return total_reserved

    # ═══════════════════════════════════════════════════════════════════════════
    # ESTRATEGIA 2: whole_lot_partial (FORMATOS/PIEZAS)
    # ═══════════════════════════════════════════════════════════════════════════

    def _reserve_whole_lot_partial(self, move, available_lots, breakdown, rounding):
        """Reserva cantidades PARCIALES según breakdown."""
        product = move.product_id
        total_reserved = 0.0

        for lot_data in available_lots:
            lot = lot_data['lot_id']
            available_qty = lot_data['available_qty']

            desired_qty = breakdown.get(lot.id)

            if desired_qty is None:
                qty_to_reserve = available_qty
                _logger.info(
                    "WholeLot[partial]: Lot %s - no breakdown, using full available %.2f",
                    lot.name, available_qty
                )
            else:
                qty_to_reserve = min(desired_qty, available_qty)
                if float_compare(qty_to_reserve, desired_qty, precision_rounding=rounding) < 0:
                    _logger.warning(
                        "WholeLot[partial]: Lot %s - desired %.2f but only %.2f available",
                        lot.name, desired_qty, available_qty
                    )
                else:
                    _logger.info(
                        "WholeLot[partial]: Lot %s - reserving %.2f (breakdown)",
                        lot.name, qty_to_reserve
                    )

            if float_compare(qty_to_reserve, 0, precision_rounding=rounding) <= 0:
                continue

            reserved = self._do_reserve_lot(move, lot, qty_to_reserve, product, rounding)
            total_reserved += reserved

        return total_reserved

    # ═══════════════════════════════════════════════════════════════════════════
    # RESERVA ATÓMICA (COMPARTIDA)
    # ═══════════════════════════════════════════════════════════════════════════

    def _do_reserve_lot(self, move, lot, qty, product, rounding):
        """Reserva `qty` del `lot` y crea el move_line. Returns: cantidad reservada."""
        Quant = self.env['stock.quant']

        try:
            reserved_before = sum(
                q.reserved_quantity
                for q in Quant._gather(product, move.location_id, lot_id=lot, strict=False)
            )

            Quant._update_reserved_quantity(
                product, move.location_id, qty, lot_id=lot, strict=False
            )

            reserved_after = sum(
                q.reserved_quantity
                for q in Quant._gather(product, move.location_id, lot_id=lot, strict=False)
            )

            actual_reserved = reserved_after - reserved_before

            if float_compare(actual_reserved, 0, precision_rounding=rounding) > 0:
                self._create_whole_lot_move_line(move, lot, actual_reserved, product)
                _logger.info(
                    "WholeLot: SUCCESS - Reserved lot '%s' (%.2f %s)",
                    lot.name, actual_reserved, product.uom_id.name
                )
                return actual_reserved
            else:
                _logger.warning(
                    "WholeLot: FAILED - Reservation had no effect for lot '%s'.", lot.name
                )
                return 0.0
        except Exception as e:
            _logger.error("WholeLot: EXCEPTION reserving lot '%s': %s",
                          lot.name if lot else 'N/A', str(e))
            return 0.0

    def _create_whole_lot_move_line(self, move, lot, quantity, product):
        """Create a stock.move.line for a whole-lot reservation."""
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