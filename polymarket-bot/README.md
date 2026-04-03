# Bots de Trading para Polymarket

Este proyecto contiene un conjunto de bots de trading diseñados para operar en la plataforma de mercados de predicción Polymarket. Los bots están optimizados para velocidad, fiabilidad y ofrecen una monitorización detallada a través de una interfaz de usuario en terminal.

---

## 🚀 crypto_bot_elite.py (Scalper Profesional de BTC y ETH a 5 Minutos)

Este bot está diseñado para **escalpear mercados de predicción de Bitcoin y Ethereum** en ciclos de 5 minutos, buscando entradas de alta probabilidad.

### Características Clave:

*   **Doble Motor de Datos**: Monitoriza `BTCUSDT` y `ETHUSDT` simultáneamente desde Binance para precios en vivo y desde Polymarket para datos de mercado.
*   **Ataque Directo por Slug**: Predice el nombre exacto (`slug`) del mercado activo de 5 minutos (ej. `btc-updown-5m-1774231200`) y lo consulta directamente en la API de Polymarket, evitando los problemas de "indexing lag" de las búsquedas generales.
*   **Target Histórico Preciso**: Si Polymarket no proporciona el `Target` (precio de inicio) explícitamente, el bot consulta el precio histórico de Binance para el segundo exacto en que inició la vela de 5 minutos.
*   **Ventana de Apuesta Continua (60s - 15s)**: No espera a un segundo exacto. El bot está activamente buscando una oportunidad para apostar tan pronto como una de las probabilidades (`UP` o `DOWN`) supere el 85% dentro de los últimos 60 a 15 segundos del mercado.
*   **Resolución Asíncrona Robusta**: Los trades se resuelven de forma inteligente. Si el bot realiza una apuesta y el mercado cierra, el bot consulta el precio histórico de cierre de Binance para determinar el WIN/LOSS exacto, incluso si no estaba "mirando" ese mercado activamente al momento del cierre.
*   **Interfaz Rich de Alta Fidelidad**: Dashboard interactivo con dos paneles (uno para BTC, otro para ETH) mostrando:
    *   Precio en vivo del activo.
    *   `Target` de Polymarket.
    *   Estado actual (`▲ ARRIBA` o `▼ ABAJO` del target).
    *   Barritas de confianza visuales para las probabilidades (`UP` y `DOWN`).
    *   Contador de tiempo restante para el cierre del mercado.
    *   Balance virtual y P&L.
    *   Estadísticas de wins/losses, ciclos totales y tasa de participación.
    *   Historial reciente de trades con P&L.
*   **Preparado para Dinero Real**: Integración con `py-clob-client` para ejecutar órdenes reales en Polymarket (se activa cambiando `SIMULATION_MODE=false` en el `.env`).

### Cómo Funciona:

1.  **Inicialización**: Al arrancar, el bot configura sus "motores" de datos para BTC y ETH.
2.  **Bucle Principal**: Cada 0.5 segundos, el bot:
    *   Consulta el precio en vivo de BTC y ETH en Binance.
    *   Calcula el timestamp del ciclo de 5 minutos actual.
    *   Para BTC y ETH, ataca directamente el `slug` de Polymarket para obtener los datos más recientes del mercado.
    *   Extrae el `Target` (precio de entrada) y los `token_ids`.
    *   Actualiza las probabilidades `UP`/`DOWN` desde el CLOB de Polymarket.
3.  **Lógica de Apuesta**: Si el tiempo restante está dentro de la "Ventana de Oportunidad" (entre 60 y 15 segundos) y un lado (`UP` o `DOWN`) supera el `THRESHOLD` (85%), el bot:
    *   Registra la apuesta en el historial.
    *   Si está en modo `REAL`, envía una orden de compra a Polymarket con un pequeño `slippage` para asegurar la ejecución.
4.  **Resolución de Trades**: Cuando un mercado finaliza (contador llega a 0), el bot:
    *   Consulta el precio histórico del activo en Binance para el momento exacto del cierre.
    *   Compara este precio con el `Target` para determinar si la apuesta fue `WIN` o `LOSS`.
    *   Actualiza el balance y los contadores de `wins`/`losses`.
5.  **Renderizado UI**: Actualiza el terminal con toda la información en tiempo real.

### Cómo Ejecutar `crypto_bot_elite.py`:

```bash
./venv/bin/python polymarket-bot/crypto_bot_elite.py
```

---

## 🕵️ copy_trader_real.py (Copy Trader de @PBot1 con Sniffer de Blockchain)

Este bot está diseñado para **copiar las compras de un trader específico** (@PBot1, o quien configures) en Polymarket, buscando ser lo más rápido posible.

### Características Clave:

*   **Sniffer de Blockchain Integrado**: Conecta directamente a la red Polygon y monitorea la dirección de @PBot1. Apenas detecta una transacción saliente de su wallet, el bot se **activa instantáneamente**.
*   **Detección de Transacción Ultra-Rápida**: Si el Sniffer detecta actividad, el bot hace múltiples consultas rápidas a la API de Polymarket para identificar el trade que acaba de realizar el trader objetivo. Esto reduce el tiempo de reacción de segundos a milisegundos.
*   **Filtrado de Compras**: Solo copia las operaciones de "compra" (`BUY`) del trader objetivo (asumiendo que quieres replicar sus entradas, no sus salidas).
*   **Balance y P&L Flotante**: Monitoriza tu balance virtual y el P&L de tus posiciones abiertas y cerradas.
*   **Interfaz Elite**: Dashboard interactivo que muestra:
    *   Balance virtual, P&L total y Wins/Losses.
    *   Panel de `Posiciones Activas` con **barras visuales** que muestran el progreso de cada trade copiado (si el precio del token sube, la barra se pone verde y crece; si baja, se pone roja y se encoge).
    *   `Historial Cerrado` con el resultado (`WIN`/`LOSS`) y el P&L de cada operación.
    *   `Log de Rastreo`: Un panel inferior que te avisa de la actividad del bot, incluyendo la detección en blockchain.
*   **Monto de Apuesta Fijo**: Configurable a través de `TRADE_SIZE_USDC` en el `.env`. El bot apostará esta cantidad fija por cada operación que copie.
*   **Preparado para Dinero Real**: Integración con `py-clob-client` para ejecutar órdenes reales en Polymarket.

### Cómo Funciona:

1.  **Inicialización**: Al arrancar, el bot inicia un hilo `Sniffer de Blockchain`.
2.  **Bucle Principal (Detección Dual)**:
    *   **Sniffer de Blockchain**: Escucha constantemente los nuevos bloques en Polygon. Si detecta una transacción de `TRADER_ADDRESS`, dispara una alerta.
    *   **Sondeo API Regular**: Si el sniffer no detecta nada o falla, el bot sigue haciendo consultas cada `POLL_INTERVAL` (30 segundos) a la API de actividad de Polymarket.
3.  **Replicación de Trade**: Cuando se detecta una compra del trader objetivo (ya sea por el Sniffer o por el sondeo normal), el bot:
    *   Registra la operación en tu `PortfolioVirtual`.
    *   Si está en modo `REAL`, envía una orden de compra a Polymarket con tus credenciales.
4.  **Monitorización Activa**: El bot actualiza constantemente los precios de las posiciones abiertas para mover las barras de progreso visuales.
5.  **Cierre y Resolución**: Si el precio de un token en tu posición abierta llega a `0.02` (casi 0) o `0.98` (casi 1), el bot asume que la posición se ha resuelto (`LOSS` o `WIN`) y la cierra, ajustando tu balance.
6.  **Renderizado UI**: Muestra el estado actualizado en el terminal.

### Cómo Ejecutar `copy_trader_real.py`:

```bash
./venv/bin/python polymarket-bot/copy_trader_real.py
```

---

## 3. Configuración General (`.env` file)

Este archivo configura el comportamiento de **todos los bots**.

```env
# 1. Modo de Operación (true para simulación, false para dinero real)
SIMULATION_MODE=true

# 2. Configuración de Apuestas
BET_AMOUNT=2.0                          # Monto de apuesta por operación (ej. 2.0 = $2 USDC)
STARTING_BALANCE=20.0                   # Saldo inicial para el modo simulación

# 3. Datos del Trader (Solo para Copy Trader)
TRADER_USERNAME=PBot1                   # Nombre de usuario del trader a seguir
TRADER_ADDRESS=0x88f46b9e5d86b4fb85be55ab0ec4004264b9d4db # Wallet Address del trader a seguir

# 4. Credenciales de Polymarket (¡SOLO PARA DINERO REAL! ¡NO COMPARTIR!)
# Necesitas una Private Key de una cuenta de Polygon con USDC y MATIC para gas.
# Las CLOB_API_KEY se generan en tu perfil de Polymarket.
PK=TU_PRIVATE_KEY_AQUI_0x...
CLOB_API_KEY=TU_API_KEY_AQUI
CLOB_SECRET=TU_SECRET_AQUI
CLOB_PASSPHRASE=TU_PASSPHRASE_AQUI

# 5. Configuración de Sondeo (Solo para Copy Trader, no tocar)
POLL_INTERVAL_SECONDS=30
```

### Importante:

*   **`SIMULATION_MODE`**: Ponlo a `false` SOLO cuando estés listo para operar con dinero real y hayas rellenado tus `PK` y `CLOB_API_KEY` en el `.env`.
*   **`TRADER_ADDRESS`**: Esta dirección es crucial para el Copy Trader. Asegúrate de que sea la correcta para el trader que quieres seguir.
*   **`PK` y `CLOB_API_KEY`**: Son tus credenciales de Polymarket. **Mantenlas en secreto** y nunca las compartas ni las subas a repositorios públicos.

---
