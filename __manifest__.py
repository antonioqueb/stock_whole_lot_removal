# -*- coding: utf-8 -*-
{
    'name': 'Estrategias de Remoción - Lotes Completos y Parciales (Sin Selección Automática)',
    'version': '19.0.2.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Dos estrategias de remoción que respetan la selección manual de lotes: una para lotes completos (placas) y otra que permite parcialidades (formatos/piezas).',
    'description': """
Estrategias de Remoción con Selección Manual Forzada
=====================================================

Este módulo provee DOS estrategias de remoción para productos donde la
selección de lotes se realiza manualmente desde la orden de venta:

1) **Lotes Completos (Sin Selección Automática)** - método `whole_lot`
   - Para productos tipo PLACA donde cada lote es indivisible.
   - Nunca divide un lote para llenar parcialmente la demanda.
   - Respeta 100% la selección manual.

2) **Lotes con Parcialidades (Sin Selección Automática)** - método `whole_lot_partial`
   - Para productos tipo FORMATO/PIEZA donde se venden cantidades parciales
     de un mismo lote.
   - Respeta 100% la selección manual.
   - Reserva las cantidades específicas por lote definidas en el breakdown
     de la línea de venta (`x_lot_breakdown_json`).
   - Si un lote seleccionado no tiene cantidad explícita, usa la cantidad
     disponible completa.

Ambas estrategias impiden que Odoo asigne lotes automáticamente por FIFO
cuando la línea de venta no tiene selección manual, dejando el movimiento
sin reservar hasta que el usuario elija los lotes.
    """,
    'author': 'Alphaqueb Consulting',
    'website': 'https://www.alphaqueb.com',
    'depends': ['stock', 'stock_transit_allocation'],
    'data': [
        'data/product_removal_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
