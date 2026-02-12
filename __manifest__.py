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
    'depends': ['stock', 'stock_transit_allocation'],
    'data': [
        'data/product_removal_data.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}
