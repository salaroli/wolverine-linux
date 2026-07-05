# wolverine-linux — Project Context

## Objetivo

Criar suporte Linux para as funcionalidades extras do Razer Wolverine Ultimate (1532:0a14):
- Botões de mídia (2 botões físicos no controle) ← **foco atual, viável**
- Headphone jack (3.5mm combo) ← **encerrado, limitação de hardware**

O gamepad em si já funciona nativamente via driver `xpad` do kernel.

---

## Hardware

**Razer Wolverine Ultimate** — USB ID `1532:0a14`  
**Hostname da máquina:** work-server  
**OS:** CachyOS (Arch-based), kernel 6.18.33-2-cachyos-lts  
**Desktop:** Hyprland + Wayland

---

## Mapa USB (descoberto via lsusb -v)

```
Interface 0  alt 0  — EP1 OUT/IN (Interrupt, 64B)   → Gamepad GIP   → driver: xpad
Interface 1  alt 0  — sem endpoints                  → idle
Interface 1  alt 1  — EP3 OUT/IN (Isochronous, 228B) → Áudio
Interface 2  alt 0  — sem endpoints                  → idle
Interface 2  alt 1  — EP2 OUT/IN (Bulk, 64B)         → Controle/eventos
```

- `bInterfaceClass 255` (Vendor Specific) em todas as interfaces
- `bInterfaceSubClass 71` (0x47) e `bInterfaceProtocol 208` (0xD0) = protocolo **Xbox GIP**

---

## Protocolo: Xbox GIP (Game Interface Protocol)

Protocolo proprietário da Microsoft para periféricos Xbox One. Publicado como open standard em setembro de 2024 (MS-GIPUSB spec no Microsoft Learn).

### Formato do pacote GIP (wire format)

```
Byte 0:    Command ID
Byte 1:    Options (bits 0-3 = client_id, bit 4 = ACK, bit 5 = INTERNAL, bit 6 = CHUNK_START, bit 7 = CHUNK)
Byte 2:    Sequence number (1-255)
Bytes 3+:  Payload length (LEB128 varint)
[chunk_offset varint — apenas se bit 7 (CHUNK) estiver setado]
[payload]
```

Header deve ter tamanho par (padding se necessário no último byte do length varint).

Para pacotes grandes (>58B): GIP usa chunking com CHUNK_START/CHUNK flags e chunk_offset.  
**Atenção:** o Wolverine envia chunks sem o flag CHUNK_START — usa chunk_offset=0 como posição inicial.

### Command IDs relevantes

| ID   | Nome              | Status no Wolverine |
|------|-------------------|---------------------|
| 0x01 | ACKNOWLEDGE       | Funciona |
| 0x02 | ANNOUNCE          | Device envia no boot (consumido pelo xpad antes de detach) |
| 0x03 | STATUS            | Device envia heartbeat a cada ~20s |
| 0x04 | IDENTIFY          | Device ignora após xpad ter feito a troca |
| 0x06 | AUTHENTICATE      | **Device não implementa** — silêncio total |
| 0x08 | AUDIO_CONTROL     | Sub 0x02 (FORMAT) funciona; sub 0x03 (VOLUME) sempre timeout |
| 0x20 | INPUT             | Reports do gamepad, 14 bytes de payload |
| 0x60 | AUDIO_SAMPLES     | Dados de áudio (isocrônico) |
| 0x0f | (Razer propietário) | Responde com cmd=0x10, propósito desconhecido |

---

## O que funciona

- **Gamepad:** 100% funcional via `xpad`. Botões, sticks, gatilhos, d-pad, guide button.
- **Gamepad via userspace (gip_init.py):** re-exposto via uinput quando detachamos o xpad.
- **GIP AUDIO_FORMAT (sub 0x02):** device ecoa confirmando o formato 48kHz stereo.
- **EP3 stream (USB):** endpoints isocrônicos abrem. Device envia 228B a ~1ms de intervalo (tudo zeros).

---

## O que NÃO funciona — conclusões definitivas

### 1. Headphone jack e microfone — ENCERRADO

**Conclusão:** limitação de hardware/firmware irreversível. O áudio é exclusivo do Xbox One.

**Evidências acumuladas:**
- Razer documenta: "game/chat volume control is only applicable for Xbox One"
- No Windows também não funciona — não existe driver PC que suporte
- EP3 IN: 1000 reads/s, todos zeros mesmo falando no mic
- EP3 OUT: enviando tom de 440Hz, nada audível nos fones
- AUDIO_CONTROL sub 0x03 (VOLUME): sempre timeout, device ignora completamente

**Investigação de GIP auth (cmd=0x06) — concluída:**
- Hipótese: o auth era o "gate" para o áudio (como no driver xone para controles Xbox)
- Implementamos o handshake TLS-like completo (RSA v1 + ECDH v2) baseado no driver xone
- Resultado: device não responde ao cmd=0x06. Silêncio absoluto durante 8 segundos de espera
- **O Wolverine não implementa GIP auth.** Usa versão simplificada do GIP sem handshake de segurança
- O bloqueio de áudio é no chip interno — o DAC/ADC só ativa com a pilha do Xbox OS

**Por que o Xbox One consegue e o Linux não:**  
O Xbox One passa um challenge criptográfico (cmd=0x0f → resposta 64B com cmd=0x10) usando chaves proprietárias do hardware do console. Sem essas chaves, o roteamento analógico interno nunca ativa. O Wolverine não usa o GIP auth padrão; usa um mecanismo Razer/Xbox proprietário diferente.

### 2. Botões de mídia — STATUS: nunca investigados

Os botões de volume/mídia físicos no controle nunca apareceram em nenhum evento capturado.

---

## Próximos passos — FOCO ATUAL: botões de mídia

### Estado da investigação

O GIP INPUT report (cmd=0x20) do Wolverine tem **14 bytes de payload** — 2 bytes a mais que o padrão Xbox (12 bytes). O `gip_init.py` já loga esses bytes extras:

```
bytes 0-1:   buttons bitmask (u16) — botões A/B/X/Y/LB/RB/start/select/etc
byte 2:      LT (0-255)
byte 3:      RT (0-255)
bytes 4-5:   LX (i16)
bytes 6-7:   LY (i16)
bytes 8-9:   RX (i16)
bytes 10-11: RY (i16)
bytes 12-13: ??? — CANDIDATOS PARA BOTÕES DE MÍDIA
```

O código em `tools/gip_init.py` já tem:
```python
if len(payload) >= 14:
    extra = payload[12:14]
    if any(extra):
        print(f"[input] EXTRA bytes 12-13: {extra.hex()} (media buttons?)")
```

### Como investigar

```bash
sudo python3 tools/gip_init.py
# Apertar os botões de volume +/- e mute/mídia do controle
# Observar se aparece: [input] EXTRA bytes 12-13: XXXX
```

Se bytes 12-13 mudarem ao pressionar os botões → temos o mapeamento.

### Plano completo (após confirmar mapeamento)

1. **Mapear os bits:** quais bits de bytes 12-13 correspondem a volume+, volume-, mute
2. **Adicionar ao uinput:** registrar `KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, `KEY_MUTE` no dispositivo virtual
3. **Criar daemon systemd:** script que detacha xpad, re-expõe gamepad + botões de mídia, inicia no boot
4. **Estrutura do daemon:** manter xpad detachado apenas enquanto daemon estiver rodando (cleanup no stop)

### Alternativa se bytes 12-13 forem sempre zero

Os botões de mídia podem aparecer em outro canal:
- EP2 IN (bulk, interface 2, alt=1) — o monitor de ctrl já está ativo no `gip_init.py`
- GIP VIRTUAL_KEY (cmd=0x07) — comando que o Wolverine pode usar para teclas extras
- Como eventos HID separados (interface diferente)

---

## Estado atual do código

### `tools/gip_init.py`

Driver userspace completo. Faz:
1. Detacha xpad de todas as interfaces
2. Cria gamepad virtual via uinput
3. Drena buffer pré-IDENTIFY
4. Envia IDENTIFY e recebe/ACKa resposta (suporte a chunks sem CHUNK_START)
5. Tenta GIP auth (falha graciosamente — device não suporta)
6. Negocia AUDIO_FORMAT 48kHz stereo
7. Ativa alt=1 nas interfaces 1 e 2
8. Monitora EP1 IN (GIP/gamepad), EP2 IN (ctrl/bulk), EP3 IN/OUT (áudio)
9. Loga bytes 12-13 dos INPUT reports para investigação de botões de mídia

### `tools/probe_gip.py` + `probe_results.log`

Probe sistemático de command IDs executado. Revelou:
- Device não responde a AUDIO_CTRL sub 0x00-0x0f (todos timeout ou ANNOUNCE enfileirado)
- CMD 0x06 (AUTH): respostas eram o ANNOUNCE enfileirado, não respostas reais
- CMD 0x0f → cmd=0x10 (propósito desconhecido, possivelmente firmware info)
- ANNOUNCE packet: `02 21 02 1c dd c7 39 dc b8 f6 00 00 32 15 17 0a...`
  - Address: `dd c7 39 dc b8 f6`
  - Vendor: 0x1532 (Razer)
  - FW version: 1.0.0.0

---

## Estrutura do repositório

```
wolverine-linux/
├── CONTEXT.md              ← este arquivo
├── README.md               ← status e documentação pública
├── probe_results.log       ← resultado do probe sistemático de commands
├── docs/
│   └── usb-analysis.md    ← análise completa dos descritores USB
└── tools/
    ├── probe.py            ← monitor passivo inicial (histórico)
    ├── probe_gip.py        ← probe sistemático de command IDs
    └── gip_init.py         ← driver userspace atual: GIP init + uinput gamepad
```

Branch atual: `feat/gip-init`

---

## Dependências

```
python-pyusb      # instalado
python-evdev      # instalado
python-cryptography  # instalado (foi necessário para implementar auth)
```

---

## Comandos úteis

```bash
# Rodar driver (investigar botões de mídia — apertar botões durante execução)
sudo python3 tools/gip_init.py

# Ver device no USB
lsusb | grep -i razer

# Ver qual driver está em cada interface
ls /sys/bus/usb/devices/ | while read d; do
  vid=$(cat /sys/bus/usb/devices/$d/idVendor 2>/dev/null)
  pid=$(cat /sys/bus/usb/devices/$d/idProduct 2>/dev/null)
  if [ "$vid" = "1532" ] && [ "$pid" = "0a14" ]; then
    ls /sys/bus/usb/devices/ | grep "^$d:" | while read iface; do
      drv=$(readlink /sys/bus/usb/devices/$iface/driver 2>/dev/null | xargs basename 2>/dev/null || echo "sem driver")
      echo "  $iface -> $drv"
    done
  fi
done

# Capturar tráfego USB (bus 1, device 16)
sudo modprobe usbmon
sudo cat /sys/kernel/debug/usb/usbmon/1t | grep ":016:" | head -40
```
