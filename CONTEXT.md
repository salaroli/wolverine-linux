# wolverine-linux — Project Context

## Objetivo

Criar suporte Linux para as funcionalidades extras do Razer Wolverine Ultimate (1532:0a14):
- Headphone jack (3.5mm combo) — saída **e** microfone ← **RESOLVIDO no nível de protocolo** (falta integração com PipeWire/ALSA)
- Botões de mídia (2 botões físicos no controle) ← **foco atual**

O gamepad em si já funciona nativamente via driver `xpad` do kernel.

> **Marco (breakthrough):** o áudio do jack **funciona no Linux**. A conclusão
> anterior de "limitação de hardware irreversível" estava **errada**. O elo perdido
> era o comando GIP `POWER` (`0x05`) com `GIP_PWR_ON` (`0x00`), enviado logo após a
> negociação de formato — exatamente como o driver [xone](https://github.com/medusalix/xone)
> faz no bring-up de headset. Sem ele o subsistema de áudio fica idle e o endpoint
> isócrono só devolve zeros. Com ele: tom de 440Hz **audível nos fones** (DAC) e mic
> **capturando** (ADC, EP3 IN com PCM real). Ver "O que funciona".

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
| 0x05 | POWER             | **CHAVE DO ÁUDIO.** `GIP_PWR_ON`=`0x00` acorda o subsistema de áudio. Faltava — nunca era enviado |
| 0x06 | AUTHENTICATE      | Device não implementa (silêncio total) — **e é irrelevante:** o caminho de jack no xone pula auth |
| 0x08 | AUDIO_CONTROL     | Sub 0x02 (FORMAT) funciona; sub 0x03 (VOLUME) dá timeout — **esperado:** xone só manda VOLUME p/ headset não-jack |
| 0x20 | INPUT             | Reports do gamepad, 14 bytes de payload |
| 0x60 | AUDIO_SAMPLES     | Dados de áudio (isocrônico) |
| 0x0f | (Razer propietário) | Responde com cmd=0x10, propósito desconhecido |

---

## O que funciona

- **Gamepad:** 100% funcional via `xpad`. Botões, sticks, gatilhos, d-pad, guide button.
- **Gamepad via userspace (gip_init.py):** re-exposto via uinput quando detachamos o xpad.
- **GIP AUDIO_FORMAT (sub 0x02):** device ecoa confirmando o formato 48kHz stereo.
- **Áudio de saída (DAC):** ✅ tom de 440Hz **audível nos fones** via EP3 OUT, após POWER ON.
- **Áudio de entrada (mic/ADC):** ✅ EP3 IN passa a mandar **PCM real** (`60 21 …` AUDIO_SAMPLES,
  amostras 16-bit LE) a ~1000 pacotes/s. Antes do POWER ON eram só zeros (stream idle).

### Sequência de bring-up de áudio que funciona (descoberta comparando com o xone)

1. IDENTIFY (+ ACK dos chunks)
2. AUDIO_FORMAT — `08` sub `02`, payload `[0x02, in=0x10, out=0x10]` (48kHz stereo). Device ecoa.
3. **POWER ON — `05`, payload `[0x00]`** ← o passo que faltava. Device responde com um
   `AUDIO_CONTROL` sub `0x00` reportando volume/mute (`04 19 19 64` = unmuted, 25/25/100).
4. Ativa alt=1 nas interfaces 1 e 2 → endpoints isócronos abrem **e transportam áudio real**.
5. VOLUME (sub 0x03) **não é enviado** no caminho de jack (flag `SEND_HW_VOLUME=False`).

Detalhe do auth: o handshake RSA/ECDH (cmd 0x06) continua sem resposta — mas isso **não bloqueia
o áudio**. No xone o caminho standalone/jack pula auth e battery. A hipótese antiga de "auth é o
gate do áudio" estava errada.

---

## Retrospectiva: a conclusão de "áudio impossível" estava errada

Esta seção documenta um erro de análise para que não se repita.

### Headphone jack e microfone — antes: "ENCERRADO"; agora: **RESOLVIDO**

Durante um bom tempo o projeto tratou o áudio como limitação de hardware irreversível.
A conclusão se apoiava em três evidências que, na verdade, eram **ambíguas** — todas
consistentes com "o subsistema nunca foi ligado", não com "o hardware é incapaz":

| Evidência de então | Interpretação correta |
|---|---|
| EP3 IN só devolve zeros mesmo falando no mic | Stream isócrono **idle** — o ADC não tinha recebido o comando de ligar (POWER ON) |
| Tom de 440Hz não sai nos fones | Roteamento de saída idle pelo mesmo motivo |
| AUDIO_CONTROL sub 0x03 (VOLUME) sempre timeout | Comportamento **normal** de jack — o xone nem envia VOLUME p/ headset de jack |

Também houve uma **contradição interna** que deveria ter acendido o alerta: dizia-se
"o áudio só liga depois do auth" **e** "o Wolverine não implementa auth" — se as duas
fossem verdade, o áudio não funcionaria nem no Xbox One. A saída do impasse foi comparar
a sequência de bring-up com o driver `xone` e notar que faltava o comando `POWER` (0x05).

**Lição:** "endpoint só manda zeros" ≠ "hardware morto". Num protocolo tipo GIP, o
periférico fica em idle até o host mandar o comando de ativação certo, endereçado ao
client id certo. Antes de declarar algo "impossível por hardware", replicar a sequência
de um driver de referência que já funciona.

### Botões de mídia — foco atual (ver "Próximos passos")

Os botões de volume/mídia físicos no controle ainda não apareceram em nenhum evento
capturado. É a próxima frente.

---

## Roadmap

1. ✅ **Áudio (protocolo)** — jack e mic funcionam via POWER ON. *Feito.*
2. ✅ **Botões de mídia** — volume e mic mute espelhados no PipeWire. *Feito.* (abaixo)
3. ⏳ **Integração de áudio com PipeWire/ALSA** — FOCO ATUAL. Transformar o I/O isócrono
   raw num sink virtual (fones) + source virtual (mic) do sistema.
4. ⏳ **Daemon systemd** — empacotar tudo (detach xpad, gamepad + botões + áudio) no boot.

## Botões de mídia — RESOLVIDO

### Onde eles realmente estão (não era onde a gente procurava)

A hipótese inicial era que os botões de volume/mídia estariam nos **bytes 12-13 do
INPUT report** (cmd 0x20). **Errado.** Aqueles bytes extras (`8dfc`, `98fb`…) são
provavelmente os paddles/botões remapeáveis traseiros — não os de mídia.

Os botões de áudio chegam pelo canal **`AUDIO_CONTROL` sub `0x00` (VOLUME_CHAT)**, no
EP1, o mesmo do gamepad. O firmware do controle mantém o estado e só reporta o resultado:

```
data[5] = mute state do mic   (0x04 unmuted / 0x05 mic-muted)
data[6] = volume absoluto      (0x00..0x64 = 0..100)
```

### Comportamento físico (manual oficial)

É **um único botão de áudio multifunção** (não +/− separados):
- **Clique** → aumenta o volume master.
- **Segurar + D-pad ↑/↓** → ajuste fino (↑ sobe, ↓ desce). É o único jeito de **baixar**.
- **Segurar + D-pad ←/→** → balanço game/chat (só Xbox One).
- **Mic mute** é um botão separado (acende quando muta).

Como o firmware resolve a combinação e reporta só o volume absoluto resultante, o driver
não precisa de lógica de "hold" — basta rastrear a direção da mudança de `data[6]`.

### Implementação (`forward_media` em gip_init.py)

- **Modo absoluto (default, `MEDIA_MODE_ABSOLUTE=True`):** espelha no PipeWire via `wpctl`
  — `set-volume @DEFAULT_AUDIO_SINK@ <v/100>` e `set-mute @DEFAULT_AUDIO_SOURCE@ 1/0`.
  O botão de volume vira o slider do sistema (sync 1:1). Roda como o usuário invocador
  (`SUDO_USER`/`XDG_RUNTIME_DIR`) porque o PipeWire vive na sessão do usuário, não do root.
- **Modo teclas (`MEDIA_MODE_ABSOLUTE=False`):** emite `KEY_VOLUMEUP/DOWN` + `KEY_MICMUTE`.
- As media keys saem de um **uinput separado só-teclado** — o libinput classifica o
  gamepad (ABS + BTN) como joystick e **engoliria** as KEY_* dele. Device keyboard puro
  é entregue ao Hyprland como teclado de verdade.
- Age só em **mudanças**; o primeiro report é baseline (conectar não puxa o volume).

---

## Próximos passos — FOCO ATUAL: integração de áudio com PipeWire

Hoje o `gip_init.py` faz I/O isócrono **raw**: lê o mic (EP3 IN) pra lugar nenhum e só
toca um tom de teste no EP3 OUT. Falta expor isso como dispositivos de áudio do sistema:

- **Sink virtual (fones):** o que o sistema tocar nesse sink → empacotar em GIP
  AUDIO_SAMPLES (cmd 0x60) e escrever no EP3 OUT.
- **Source virtual (mic):** os AUDIO_SAMPLES que chegam no EP3 IN → decodificar (PCM
  16-bit LE, 48kHz stereo) e empurrar pro source.
- **Como conectar:** provavelmente via módulo `pipewire`/`pw-cli` ou um `snd-aloop` +
  bridge, ou um cliente PipeWire nativo. Avaliar a abordagem mais simples e estável.
- Cuidar de formato/timing (1000 pacotes/s, ~1ms) e latência.

---

## Estado atual do código

### `tools/gip_init.py`

Driver userspace completo. Faz:
1. Detacha xpad de todas as interfaces
2. Cria gamepad virtual via uinput + um uinput separado só-teclado p/ media keys
3. Drena buffer pré-IDENTIFY
4. Envia IDENTIFY e recebe/ACKa resposta (suporte a chunks sem CHUNK_START)
5. Tenta GIP auth (falha graciosamente — device não suporta, e não é necessário p/ jack)
6. Negocia AUDIO_FORMAT 48kHz stereo
7. **Envia POWER ON (`pkt_power`, cmd 0x05) — acorda o áudio.** VOLUME fica sob flag `SEND_HW_VOLUME`
8. Ativa alt=1 nas interfaces 1 e 2
9. Monitora EP1 IN (GIP/gamepad), EP2 IN (ctrl/bulk), EP3 IN/OUT (áudio — agora com PCM real)
10. **Botões de mídia:** `forward_media()` espelha volume/mic mute no PipeWire (via `wpctl`)
    a partir dos reports `AUDIO_CONTROL` sub 0x00

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
