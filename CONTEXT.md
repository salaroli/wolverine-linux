# wolverine-linux — Project Context

## Objetivo

Criar suporte Linux para as funcionalidades extras do Razer Wolverine Ultimate (1532:0a14):
- Headphone jack (3.5mm combo — áudio + microfone)
- Botões de mídia (2 botões físicos no controle)

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

Protocolo proprietário da Microsoft para periféricos Xbox One.

### Formato do pacote GIP

```
Byte 0:    Command ID
Byte 1:    Options (bits 0-3 = client_id, bit 4 = ACK, bit 5 = INTERNAL, bit 6/7 = CHUNK)
Byte 2:    Sequence number (1-255)
Bytes 3+:  Payload length (LEB128 varint)
[payload]
```

Header deve ter tamanho par (padding se necessário).

### Command IDs relevantes

| ID   | Nome              | Observação |
|------|-------------------|------------|
| 0x01 | ACKNOWLEDGE       | |
| 0x03 | STATUS            | Device envia heartbeat a cada ~20s |
| 0x04 | IDENTIFY          | Host → device, device responde com STATUS (0x03) |
| 0x08 | AUDIO_CONTROL     | Negociação de áudio (subcomandos abaixo) |
| 0x20 | INPUT             | Reports do gamepad |
| 0x60 | AUDIO_SAMPLES     | Dados de áudio (isocrônico) |

### AUDIO_CONTROL subcomandos

| Sub  | Nome              | Observação |
|------|-------------------|------------|
| 0x00 | VOLUME_CHAT       | Device envia espontaneamente ao plugar/despluguar jack |
| 0x01 | FORMAT_CHAT       | Formato chat (mono, 16/24kHz) |
| 0x02 | FORMAT            | Formato padrão — **funciona: device ecoa confirmando** |
| 0x03 | VOLUME            | Volumes — **sempre timeout, device ignora** |

### Formato AUDIO_FORMAT (subcomando 0x02)

```
Packet: 08 21 seq 03 02 [in_fmt] [out_fmt]
opts = 0x21 = GIP_OPT_INTERNAL | client_id=1
```

Formato 48kHz stereo = `0x10`. Device **sempre ecoa** confirmando aceitação.

### Audio format codes

| Code | Formato           |
|------|-------------------|
| 0x05 | 16kHz mono        |
| 0x09 | 24kHz mono        |
| 0x10 | 48kHz stereo      |

---

## O que funciona

- **Gamepad:** 100% funcional via `xpad`. Botões, sticks, gatilhos, d-pad, guide button.
- **GIP na interface 0:** protocolo ativo. IDENTIFY, STATUS, AUDIO_FORMAT todos funcionam.
- **Stream EP3 IN:** confirmado via usbmon — device envia 228 bytes a ~1ms de intervalo quando alt=1 está ativo. Dados são zeros (silêncio).
- **Stream EP3 OUT:** pyusb consegue enviar ~1000 pacotes/s sem erros.

---

## O que NÃO funciona e por quê

### Headphone jack e microfone

**Conclusão:** limitação de hardware/firmware — áudio é **exclusivo do Xbox**, não funciona em PC. **DEFINITIVAMENTE ENCERRADO.**

**Evidências:**
- Razer documenta oficialmente: "game/chat volume control is only applicable for Xbox One"
- No Windows, o áudio também não funciona — não existe driver PC que suporte
- O stream isocrônico EP3 abre (protocolo USB ok), mas o roteamento interno (DAC/ADC) nunca ativa
- Enviar tom de 440Hz no EP3 OUT: não audível nos fones
- Microfone: 1000 reads/s no EP3 IN, todos zeros, mesmo falando no mic
- Comando VOLUME (sub 0x03): sempre timeout, device ignora

**Investigação de auth GIP (cmd=0x06) — concluída:**
- O device **não implementa GIP auth** (cmd=0x06). Silêncio total de 8 segundos após HOST_HELLO.
- A hipótese de que auth era o gate para o áudio foi descartada.
- O device usa versão simplificada de GIP sem handshake de autenticação.
- O bloqueio de áudio é no firmware/hardware, independente de auth.

**Investigação de GIP IDENTIFY:**
- O device responde ao IDENTIFY com STATUS heartbeat (seq=71), não com IDENTIFY response.
- O seq=71 indica que xpad já fez ~70 trocas antes de ser detachado — device está em estado estabelecido.
- Para capturar o IDENTIFY response completo seria necessário reset USB antes de clamar as interfaces.

**Comandos Razer proprietários (descobertos no probe):**
- CMD 0x0f → resposta de 64B com cmd=0x10 (propósito desconhecido — pode ser info de firmware)
- Esses são comandos Razer-específicos fora do spec GIP padrão.

### Botões de mídia

**Status:** desconhecido — nunca apareceram em nenhum evento capturado.

**Hipótese:** podem aparecer como flags extras nos GIP INPUT reports (EP1 IN, 0x20 command).  
O report de INPUT do Wolverine tem 14 bytes de payload (mais que o padrão de 12) — os 2 bytes extras podem conter os botões de mídia.

---

## Próximos passos

### 1. Investigar botões de mídia (viável)

O GIP INPUT report (cmd=0x20) recebido via EP1 IN tem 14 bytes de payload:
```
bytes 0-1:  buttons bitmask (u16)
byte 2:     LT
byte 3:     RT
bytes 4-5:  LX (i16)
bytes 6-7:  LY (i16)
bytes 8-9:  RX (i16)
bytes 10-11:RY (i16)
bytes 12-13: ??? (possivelmente botões extras / botões de mídia)
```

**Plano:**
1. Detachar xpad, claim interface 0
2. Ler EP1 IN continuamente
3. Pressionar os botões de mídia e observar diferença nos bytes 12-13
4. Mapear para eventos de sistema: `pactl set-sink-volume @DEFAULT_SINK@ +5%` etc.

### 2. Exposição via uinput (depois de mapear os botões)

Usar `evdev.UInput` para criar dispositivo de input virtual com os botões de mídia mapeados como `KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, etc.

### 3. Daemon persistente

Após descobrir o mapeamento dos botões de mídia, criar um daemon que:
- Detacha xpad e re-expõe gamepad via uinput
- Monitora botões de mídia e executa ações de volume do sistema
- Inicia automaticamente via systemd unit

---

## Estrutura do repositório

```
wolverine-linux/
├── CONTEXT.md              ← este arquivo
├── README.md               ← status e documentação pública
├── docs/
│   └── usb-analysis.md    ← análise completa dos descritores USB
└── tools/
    ├── probe.py            ← monitor passivo inicial (histórico)
    ├── gip_init.py         ← driver userspace atual: GIP init + uinput gamepad
    └── probe_gip.py        ← probe sistemático de command IDs (ainda não executado)
```

Branch atual: `feat/gip-init`

---

## Dependências

```
python-pyusb    # instalado
python-evdev    # instalado
```

---

## Comandos úteis de diagnóstico

```bash
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

# Rodar driver userspace
sudo python3 tools/gip_init.py
```
