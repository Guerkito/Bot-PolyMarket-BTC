# Estado del Proyecto: Crypto Bot Elite — Polymarket

## ¿Qué es este bot?
Bot de trading automatizado para Polymarket (mercados de predicción). Opera en mercados de BTC y ETH de 5 minutos apostando UP o DOWN según probabilidades del CLOB (order book).

## Archivos clave
- `crypto_bot_elite.py` — bot principal
- `.env` — credenciales y configuración
- `get_credentials.py` — script para generar credenciales CLOB
- `trades_history.json` — historial de trades
- `diagnose_clob.py` — script de diagnóstico para verificar balance, allowance y conectividad.

---
## Diagnóstico Reciente: Problema de Saldo Cero (24/Mar/2026)

### Problema Reportado
El bot reportaba un saldo de **0 USDC**, impidiendo la operación. Sin embargo, al verificar en PolygonScan, la wallet `0x171874eEFFD596E901f4B6573464F2C20F3d359E` mostraba un saldo de **~20.59 USDC.e**.

### Proceso de Investigación
1.  **Hipótesis Inicial:** Se pensó que los fondos podrían estar en un "Proxy Wallet" no detectado por el bot.
2.  **Intento 1:** Se intentó usar la función `client.get_proxy_address()`, pero falló con `AttributeError`, indicando que el método no existe en la versión del SDK (`py_clob_client`) instalada en el `venv`.
3.  **Intento 2:** Se consultó la API pública de Polymarket, pero devolvió un error de decodificación JSON, sugiriendo que la respuesta no era la esperada o requería autenticación.
4.  **Creación de `diagnose_clob.py`:** Se creó un script para realizar 4 verificaciones clave solicitadas:
    *   Dirección exacta usada para consultar el saldo.
    *   Contrato del token consultado.
    *   Allowance del token hacia el exchange.
    *   Respuesta cruda de `post_order()`.
5.  **Resultados del Diagnóstico:**
    *   ✅ **Dirección correcta:** El bot está configurado para usar `FUNDER="0x171874eEFFD596E901f4B6573464F2C20F3d359E"`.
    *   ✅ **Token correcto:** Se consulta el contrato `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (USDC.e).
    *   ✅ **API Funcional:** El intento de `post_order()` devolvió `{'error': 'market not found'}`, lo cual es correcto, pues se usó un ID de mercado de prueba. Confirma que la autenticación y conexión con la API funcionan.
    *   ❌ **Error Crítico:** La llamada a `client.get_balance()` falló con `AttributeError`.

### Causa Raíz Identificada
La versión de la librería `py_clob_client` instalada en el entorno virtual no contiene los métodos `get_balance()` ni `get_allowance()`. Tras inspeccionar los métodos disponibles, se descubrió que la función correcta para esta versión es **`get_balance_allowance()`**.

Este método unificado consulta tanto el saldo como el allowance en una sola llamada a la API de Polymarket.

---
## Estado Actual y Próximos Pasos

### Bot
- **Conectividad:** OK. El bot se autentica y puede comunicarse con la API de Polymarket.
- **Lógica de Órdenes:** OK. El mecanismo para firmar y enviar órdenes es funcional.
- **Lectura de Saldo:** **Incorrecta.** El código actual usa métodos (`get_balance`, `get_allowance`) que deben ser reemplazados por `get_balance_allowance()`.

### Configuración actual del bot (Cargar desde .env)
```
SIMULATION_MODE=false
STARTING_BALANCE=20.57
# Configurar estas variables en el archivo .env
TRADER_ADDRESS=TU_DIRECCION_PUBLICA
PK=TU_CLAVE_PRIVADA
CLOB_API_KEY=TU_API_KEY
# ... resto de credenciales
```

### Cómo se inicializa el cliente CLOB (debe permanecer así)
```python
creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
self.client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds,
                         funder=FUNDER, signature_type=2) # Signature type 2 es crucial
```

### Solución Pendiente
Actualizar todos los scripts que consultan el saldo (`crypto_bot_elite.py`, `check_balance.py`, etc.) para que dejen de usar `get_balance()` y en su lugar utilicen:

```python
# Ejemplo de cómo obtener el saldo y allowance
try:
    balance_info = client.get_balance_allowance(USDC_E_ADDRESS)
    balance = balance_info['balance']
    allowance = balance_info['allowance']
    print(f"Saldo: {balance}, Allowance: {allowance}")
except Exception as e:
    print(f"Error: {e}")

```
Una vez corregido esto, el bot podrá ver el saldo correctamente y empezar a operar.

---

## Cómo correr el bot (después de corregir el código)
```bash
cd "/home/guerk/Bot Claude/polymarket-bot" && source ../venv/bin/activate && python crypto_bot_elite.py
```
