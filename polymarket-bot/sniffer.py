import time
import sys
from datetime import datetime
from web3 import Web3
from dotenv import load_dotenv
import os

# Cargar configuración
load_dotenv()

# Configuración
TARGET_ADDRESS = "0x88f46b9e5d86b4fb85be55ab0ec4004264b9d4db" # @PBot1
# RPC Público de Polygon (Para producción, usa uno privado como Alchemy o Infura)
POLYGON_RPC = "https://polygon-rpc.com"

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.live import Live
    from rich.table import Table
    console = Console()
    RICH = True
except:
    RICH = False

class BlockchainSniffer:
    def __init__(self):
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        self.target = Web3.to_checksum_address(TARGET_ADDRESS)
        self.last_block = self.w3.eth.block_number
        self.tx_found = 0
        
    def connect(self):
        if self.w3.is_connected():
            if RICH: console.print(f"[bold green]✅ Conectado a Polygon Blockchain[/] (Bloque: {self.last_block})")
            return True
        else:
            if RICH: console.print("[bold red]❌ Error conectando al Nodo RPC[/]")
            return False

    def scan_block(self, block_num):
        try:
            # Obtener bloque completo con transacciones
            block = self.w3.eth.get_block(block_num, full_transactions=True)
            timestamp = datetime.fromtimestamp(block.timestamp).strftime('%H:%M:%S')
            
            # Filtrar transacciones del objetivo
            for tx in block.transactions:
                if tx['from'] == self.target:
                    self.alert_tx(tx, timestamp)
                    
        except Exception as e:
            print(f"Error leyendo bloque {block_num}: {e}")

    def alert_tx(self, tx, time_str):
        self.tx_found += 1
        hash_link = f"https://polygonscan.com/tx/{tx['hash'].hex()}"
        
        # Intentar adivinar la acción por el Input Data
        action = "Interacción Desconocida"
        if tx['input'].startswith('0x'):
            # Firmas comunes de Polymarket/CTF
            if tx['to'] == "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E": # Exchange Contract
                action = "🔄 OPERACIÓN EN POLYMARKET"
            elif tx['to'] == "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296": # Gnosis Safe (Proxy)
                action = "🛡️ PROXY TRADE / ORDEN"
        
        if RICH:
            console.print(Panel(
                f"[bold yellow]🔔 ACTIVIDAD DETECTADA EN BLOCKCHAIN[/]\n"
                f"Hora: [cyan]{time_str}[/]\n"
                f"Acción: [bold green]{action}[/]\n"
                f"Hash: [blue]{tx['hash'].hex()[:10]}...[/]\n"
                f"[link={hash_link}]Ver en PolygonScan[/link]",
                border_style="red",
                title="🚨 ALERTA TEMPRANA"
            ))
        else:
            print(f"🔔 DETECTADO: {action} | Hash: {tx['hash'].hex()}")

    def run(self):
        if not self.connect(): return
        
        if RICH: console.print(f"[dim]Espiando a: {self.target}[/dim]")
        if RICH: console.print("[bold cyan]Esperando nuevos bloques...[/bold cyan]")

        while True:
            try:
                current_block = self.w3.eth.block_number
                
                if current_block > self.last_block:
                    # Escanear bloques nuevos (pueden ser varios si hubo lag)
                    for b in range(self.last_block + 1, current_block + 1):
                        self.scan_block(b)
                        print(f"\rEscaneado bloque: {b}", end="", flush=True)
                    
                    self.last_block = current_block
                
                time.sleep(2) # Polygon genera bloques cada ~2.1s
                
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error loop: {e}")
                time.sleep(5)

if __name__ == "__main__":
    sniffer = BlockchainSniffer()
    sniffer.run()
