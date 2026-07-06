# wolverine-linux — Project Context

## Objetivo

Criar suporte Linux para as funcionalidades extras do Razer Wolverine Ultimate (1532:0a14):
- Headphone jack (3.5mm combo) — saída **e** microfone ← **funciona** (protocolo + PipeWire); refinando qualidade
- Botões de mídia (2 botões físicos no controle) ← **RESOLVIDO** (espelhados no PipeWire)

O gamepad em si já funciona nativamente via driver `xpad` do kernel.

### Status resumido (atualizar aqui a cada avanço)

| Frente | Status |
|---|---|
| Gamepad | ✅ funciona (xpad + re-exposto via uinput) |
| Áudio saída (fones) | 🟡 funciona via PipeWire (sink *Wolverine Headphones*), mas com **voz robótica** (ver Próximos passos) |
| Áudio entrada (mic) | 🟡 funciona via PipeWire (source *Wolverine Microphone*), formato **24kHz mono** confirmado |
| Botões de mídia | ✅ volume + mic mute espelhados no PipeWire |
| **Voz robótica** (saída) | ❌ **PENDENTE** — iso síncrono tem micro-gaps; fix = iso assíncrono (ver Próximos passos) |
| **Buzz canal esquerdo** | ❌ **PENDENTE** — é hardware/analógico; só mitigável (ver Próximos passos) |

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
2. ✅ **Botões de mídia** — volume e mic mute espelhados no PipeWire. *Feito.*
3. ✅ **Integração de áudio com PipeWire** — sink + source nativos via shim C. *Feito, com 2 bugs abertos.*
4. ❌ **Corrigir voz robótica** (iso assíncrono) — **PRÓXIMO FOCO**. Ver seção dedicada.
5. ❌ **Buzz canal esquerdo** (hardware) — mitigação opcional. Ver seção dedicada.
6. ⏳ **Daemon systemd** — empacotar tudo (detach xpad, gamepad + botões + áudio) no boot.

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

## Integração de áudio com PipeWire — FEITO (via shim C nativo)

Escolhemos **cliente PipeWire nativo** (não snd-aloop, não CLI). Como as bindings Python
nativas não existem (o `pipewire_python` é wrapper de CLI, e `spa_format_audio_raw_build`
é `static inline` — inacessível via ctypes), a solução foi um **shim em C**.

### `tools/wolverine_pw.c` (compilado p/ `wolverine_pw.so` via `tools/Makefile`)

- Cria 2 nós no grafo: **Wolverine Headphones** (Audio/Sink) e **Wolverine Microphone**
  (Audio/Source), usando `pw_stream` + `pw_thread_loop`.
- Dois **ring buffers** thread-safe (mutex, drop-on-overflow) fazem a ponte entre a thread
  RT do PipeWire e as threads USB do Python.
- API exposta via ctypes: `wpw_start(out_rate,out_ch,in_rate,in_ch)`, `wpw_stop()`,
  `wpw_playback_avail()`, `wpw_read_playback(dst,len)`, `wpw_write_capture(src,len)`.
- Sink e source têm **formatos independentes** (o mic não é igual à saída).

### Aprendizado crítico: framing do EP3 (as duas direções são DIFERENTES)

- **EP3 OUT (fones):** **PCM cru**, 192 bytes por pacote (48 frames S16LE **48kHz stereo**),
  ~1000 pacotes/s. **NÃO tem header.** O código antigo mandava um prefixo `<u16 192>`
  (= bytes `c0 00`) achando que era "length" — mas o device **tocava esses 2 bytes como
  PCM**, causando (a) buzz no canal esquerdo e (b) 194 bytes = 48,5 frames → desalinhamento
  = crackle. Remover o prefixo (PCM cru) **matou o crackle**. Flag `WOLV_OUT_HEADER=1`
  restaura o prefixo antigo só p/ teste.
- **EP3 IN (mic):** **GIP-framed** `60 21 <seq> <len> | <2B sub-header> | <PCM>`. O PCM é
  **24kHz mono** (confirmado: ~48000 bytes/s ÷ 2 = 24000 amostras/s). Parseado com
  `decode_gip_header()`, pulando o sub-header de 2 bytes.

### Como rodar

```bash
make -C tools                       # compila wolverine_pw.so (precisa headers do pipewire)
sudo python3 tools/gip_init.py      # com fones no jack
# nós aparecem em `wpctl status`; testar com pw-play/pw-record --target wolverine_*
```
Detalhe: o shim conecta no PipeWire da **sessão do usuário** (aponta `XDG_RUNTIME_DIR`
via `SUDO_UID`), senão os nós iriam pro root.

---

## ❌ PRÓXIMO PASSO 1 — Corrigir a voz robótica (saída)

### Diagnóstico (fechado, com dados)

Instrumentei o `stream_audio_out`. Com áudio tocando:
```
1000 pkt/s, 0 underruns/5s, ring 4480-5568B (~28ms, estável)
```
**O nosso lado está impecável:** pacing 1000/s certo, zero underrun, ring saudável.
Mesmo assim a voz é robótica. Isso **elimina** buffer/PipeWire/ring e aponta pra **única
camada restante: o transporte isócrono USB**.

**Causa raiz:** usamos `dev.write` **síncrono, 1 pacote por vez**. Entre uma transferência
terminar e a próxima ser submetida há um **micro-gap** (overhead do loop Python + GIL).
Endpoint isócrono é implacável: frame de 1ms sem pacote = buraco. Espalhado numa voz =
timbre robótico. (Já testamos: PCM cru vs prefixo, colchão 8ms vs 20ms, re-prime on/off —
nada disso resolve, porque o problema está **abaixo** do nosso buffer.)

### A correção (decidida): iso ASSÍNCRONO com fila de transferências

Manter **N buffers (8–16) sempre em voo**, resubmetidos em callback, pro controlador de USB
nunca ficar sem pacote no próximo frame.

**Abordagem escolhida: opção #1 — `python-libusb1` (`usb1`) só para o EP3.**
- Mantém o `pyusb` pro resto (EP1 GIP, EP2). Segundo handle reivindicando **só a interface 1**.
- Fica tudo em Python; o shim C continua só PipeWire.
- Reescrever `stream_audio_out` e `monitor_audio` p/ transferências iso assíncronas em fila,
  alimentando os **mesmos rings do C** (`wpw_read_playback`/`wpw_write_capture`).
- Rodar uma thread de event loop do libusb (`usb1` `handleEvents`).
- Cuidado: coordenar com o pyusb — hoje o `main` reivindica a interface 1; passar essa posse
  pro `usb1`. A negociação GIP (POWER/FORMAT) no EP1 acontece ANTES, então a ordem importa.

Alternativa registrada (não escolhida): mover o EP3 pro C com libusb async (mais coeso com
os rings, mas adiciona libusb ao build C e coordenação de handle).

---

## ❌ PRÓXIMO PASSO 2 — Buzz no canal esquerdo (hardware)

**Veredito: é analógico/hardware, não está no nosso sinal.** Provas:
- Com PCM **cru de zeros** (silêncio digital perfeito), o buzz **continua**.
- **Escala com o botão físico** de volume (ganho analógico), não com volume digital.
- Só no canal **esquerdo** → desbalanço/ruído no amp do controle (provável aterramento do
  jack combo ou ruído da alimentação USB acoplado no analógico).

**Não dá pra consertar no PCM.** Única alavanca de software:
- **Parar o stream isócrono quando o áudio está idle** (nenhum app tocando no sink) → o
  DAC/amp quiesce e o buzz some quando nada toca; volta ao tocar (com possível "pop" e uns
  ms de latência no início). Precisa detectar o estado idle do sink (via PipeWire no shim C,
  ex: contar consumidores/atividade) e pausar/retomar o envio no EP3 OUT.
- Decisão pendente do usuário: se vale implementar isso ou tocar o barco (é hardware).

---

## Estado atual do código (arquivos novos desta fase)

- **`tools/wolverine_pw.c`** — shim PipeWire nativo (sink+source, rings). Compila limpo com
  PipeWire 1.6.6.
- **`tools/Makefile`** — `make -C tools` gera `wolverine_pw.so` (gitignored).
- **`tools/gip_init.py`** — integrado: `load_pipewire_bridge()` (ctypes), `stream_audio_out`
  (drena sink→EP3 OUT, PCM cru, priming, diagnóstico pkt/s+underrun), `monitor_audio`
  (EP3 IN→source, parse GIP, diagnóstico bytes/s), `forward_media` (botões).
- Constantes de formato: `OUT_RATE=48000/OUT_CHANNELS=2`, `IN_RATE=24000/IN_CHANNELS=1`.

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
9. **Inicia o bridge PipeWire** (`load_pipewire_bridge` + `wpw_start`) → sink + source
10. Monitora EP1 IN (GIP/gamepad), EP2 IN (ctrl/bulk); `stream_audio_out` drena sink→EP3 OUT
    e `monitor_audio` empurra EP3 IN→source (⚠️ iso síncrono = voz robótica, ver Passo 1)
11. **Botões de mídia:** `forward_media()` espelha volume/mic mute no PipeWire (via `wpctl`)
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
    ├── gip_init.py         ← driver userspace: GIP init + uinput + áudio + botões
    ├── wolverine_pw.c      ← shim PipeWire nativo (sink+source, rings)
    ├── Makefile            ← gera wolverine_pw.so
    └── wolverine_pw.so     ← binário compilado (gitignored)
```

Branch atual: `feat/gip-init`

---

## Dependências

```
python-pyusb         # instalado
python-evdev         # instalado
python-cryptography  # instalado (foi necessário para implementar auth)
pipewire + headers   # instalado (Arch: no pacote `pipewire`); p/ compilar wolverine_pw.so
wpctl                # instalado (botões de mídia)
python-libusb1       # ⚠️ A INSTALAR — necessário p/ o Passo 1 (iso assíncrono)
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
