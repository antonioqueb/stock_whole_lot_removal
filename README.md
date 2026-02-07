# Estrategia de Remoción: Lotes Completos (Sin Selección Automática)

## Descripción

Módulo para Odoo 19 que agrega una nueva **estrategia de remoción forzada** diseñada para productos vendidos por lotes completos (placas de piedra, mármol, granito, etc.).

## Problema que resuelve

En Odoo estándar, las estrategias de remoción (FIFO, LIFO, etc.) dividen cantidades entre lotes para completar la demanda. Por ejemplo:

- Demanda: 15 m²
- Lote A: 8 m², Lote B: 6 m², Lote C: 10 m²
- **FIFO estándar**: Toma 8 m² del Lote A + 7 m² del Lote C = ❌ ¡Lote C dividido!

Esto es inaceptable para placas de piedra/mármol donde cada lote es una placa entera e indivisible.

## Comportamiento de "Lotes Completos"

Con esta estrategia:
- **Solo reserva lotes cuya cantidad completa se puede usar**
- **NUNCA divide un lote** para llenar parcialmente la demanda
- Si no encuentra combinación exacta, reserva lo que pueda con lotes completos
- El resto queda sin reservar para selección manual del usuario

### Ejemplo:
- Demanda: 15 m²
- Lote A: 8 m², Lote B: 6 m², Lote C: 10 m²
- **Resultado**: Reserva Lote A (8 m²) + Lote B (6 m²) = 14 m² ✅
- Los 1 m² restantes quedan para selección manual

## Instalación

1. Copiar la carpeta `stock_whole_lot_removal` al directorio de addons
2. Reiniciar Odoo: `./odoo-bin -u stock_whole_lot_removal`
3. Activar el módulo desde Aplicaciones

## Configuración

1. Ir a **Inventario → Configuración → Categorías de producto**
2. Seleccionar la categoría deseada
3. En **Estrategia de remoción forzada**, seleccionar **"Lotes Completos (Sin Selección Automática)"**

También se puede configurar a nivel de ubicación:
1. Ir a **Inventario → Configuración → Ubicaciones**
2. Seleccionar la ubicación
3. En **Estrategia de remoción**, seleccionar **"Lotes Completos (Sin Selección Automática)"**

## Requisitos

- Los productos deben estar configurados con seguimiento **"Por lotes"**
- Tener habilitado **Números de lote y serie** en configuración de inventario

## Dependencias

- `stock` (Inventario)

## Autor

Alphaqueb Consulting SAS
