# 🤖 Bot PolyMarket BTC - 2026 Edition

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)

Este es un bot de trading de alta precisión diseñado para operar en **PolyMarket** (Polygon Network). Está especializado en mercados de predicción de **Bitcoin (BTC)** con intervalos de 5 minutos, utilizando estrategias de momentum y análisis de order book en tiempo real.

---

## 🚀 Características Principales

*   **⚡ Ejecución Ultra-Rápida:** Optimizado para ventanas de tiempo críticas (50s a 10s antes del cierre).
*   **📊 Estrategia de Momentum:** Análisis de ticks de precio de Binance (WebSocket) para confirmación de tendencia.
*   **🛡️ Gestión de Riesgos:** Límite de pérdidas consecutivas, control de balance y techos de probabilidad (Max Entry Price).
*   **🕵️ Shadow Mode:** Sistema de simulación avanzada para probar estrategias sin arriesgar capital real.
*   **🔔 Alertas en Tiempo Real:** Integración con Telegram para notificaciones de operaciones y estado del bot.
*   **📜 Registro Detallado:** Logs de operaciones y exportación de análisis a Excel/CSV.

---

## 🛠️ Requisitos e Instalación

1.  **Clonar el repositorio:**
    ```bash
    git clone https://github.com/Guerkito/Bot-PolyMarket-BTC.git
    cd Bot-PolyMarket-BTC
    ```

2.  **Crear y activar entorno virtual:**
    ```bash
    python -m venv venv
    source venv/bin/activate  # En Windows: venv\Scripts\activate
    ```

3.  **Instalar dependencias:**
    ```bash
    pip install -r requirements.txt
    ```

---

## ⚙️ Configuración Segura

El bot utiliza un archivo `.env` para gestionar las credenciales. **Nunca compartas este archivo ni lo subas a GitHub.**

Crea un archivo `.env` en la raíz con el siguiente formato:

```env
# Credenciales de Polygon
PK=tu_clave_privada_aqui
TRADER_ADDRESS=tu_direccion_publica_aqui

# API de PolyMarket (CLOB)
CLOB_API_KEY=tu_api_key
CLOB_SECRET=tu_api_secret
CLOB_PASSPHRASE=tu_passphrase

# Configuración de Trading
SIMULATION_MODE=true  # Cambiar a false para operar real
BET_AMOUNT=2.0
STARTING_BALANCE=20.0
MAX_LOSS_STREAK=4

# Notificaciones
TELEGRAM_TOKEN=tu_token_de_bot
TELEGRAM_CHAT_ID=tu_id_de_chat
```

---

## 📂 Estructura del Proyecto

*   `polymarket-bot/crypto_bot_elite.py`: El núcleo del bot con la lógica de trading más avanzada.
*   `polymarket-bot/simulador.py`: Script para pruebas sin conexión a la API real.
*   `polymarket-bot/sniffer.py`: Herramienta para monitorear ballenas y otros traders en PolyMarket.
*   `polymarket-bot/get_credentials.py`: Utilidad para derivar credenciales de la API de PolyMarket.

---

## ⚠️ Descargo de Responsabilidad

Este software es para fines educativos y de investigación. El trading en mercados de predicción conlleva riesgos significativos. **No inviertas dinero que no puedas permitirte perder.** El autor no se hace responsable de las pérdidas financieras derivadas del uso de este bot.

---

## 📝 Licencia

Distribuido bajo la Licencia MIT. Ver `LICENSE` para más información.
