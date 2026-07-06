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
| Áudio saída (fones) | ✅ **voz limpa** via PipeWire (sink *Wolverine Headphones*) — iso assíncrono + enquadramento GIP |
| Áudio entrada (mic) | ✅ funciona via PipeWire (source *Wolverine Microphone*), formato **24kHz mono** confirmado |
| Botões de mídia | ✅ volume + mic mute espelhados no PipeWire |
| ~~Voz robótica~~ (saída) | ✅ **RESOLVIDO** — a causa era **formato** (faltava header GIP no OUT), não timing (ver seção dedicada) |
| ~~Buzz canal esquerdo~~ | ✅ **RESOLVIDO** — sumiu junto com o fix do enquadramento GIP; **não era hardware** (ver seção dedicada) |

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
4. ✅ **Voz robótica corrigida** — iso assíncrono (usb1) **+ enquadramento GIP no OUT**. *Feito.* Ver seção dedicada.
5. ✅ **Buzz canal esquerdo** — sumiu junto com o fix do enquadramento GIP (não era hardware). *Feito.* Ver seção dedicada.
6. ⏳ **Daemon systemd** — empacotar tudo (detach xpad, gamepad + botões + áudio) no boot. **PRÓXIMO FOCO.**

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

### Aprendizado crítico: framing do EP3 (as duas direções são GIP, quase simétricas)

- **EP3 OUT (fones):** **GIP-framed** `60 21 <seq> <len=192>` + **192B de PCM** (48 frames
  S16LE **48kHz stereo**), ~1000 pacotes/s → **198B por pacote iso**. A `seq` incrementa
  1..255 (nunca 0), uma por pacote. **NÃO é PCM cru** — mandar PCM cru = voz robótica
  (o firmware desincroniza sem o header). Espelha `gip_copy_audio_samples()` do xone.
  *Histórico:* o antigo prefixo `<u16 192>` (= `c0 00`) era um header errado (faltava
  `60 21 <seq>`); tocava como PCM → buzz/crackle. "PCM cru" só soava menos pior. O header
  GIP completo `60 21 <seq> c0 81 00` foi o que resolveu.
- **EP3 IN (mic):** **GIP-framed** `60 21 <seq> <len> | <2B length_out le16> | <PCM>`. O
  sub-header de 2 bytes é o `__le16 length_out` do `struct gip_pkt_audio_samples` do xone.
  PCM é **24kHz mono** (confirmado: ~48000 bytes/s ÷ 2 = 24000 amostras/s). Parseado com
  `decode_gip_header()`, pulando os 2 bytes. **Diferença OUT↔IN:** o IN tem o `length_out`
  le16 depois do header GIP; o OUT não (header GIP + PCM direto).

### Como rodar

```bash
make -C tools                       # compila wolverine_pw.so (precisa headers do pipewire)
sudo python3 tools/gip_init.py      # com fones no jack
# nós aparecem em `wpctl status`; testar com pw-play/pw-record --target wolverine_*
```
Detalhe: o shim conecta no PipeWire da **sessão do usuário** (aponta `XDG_RUNTIME_DIR`
via `SUDO_UID`), senão os nós iriam pro root.

---

## ✅ RESOLVIDO — Voz robótica (saída)

A voz saiu **limpa** com duas mudanças combinadas. A segunda foi a que realmente resolveu;
a primeira eliminou uma variável e produziu a evidência que apontou pra causa certa.

### O erro de diagnóstico (documentado pra não repetir)

O diagnóstico anterior fechou na conclusão **errada**: "iso síncrono → micro-gaps →
voz robótica". Baseava-se em: `stream_audio_out` mostrava `1000 pkt/s, 0 underruns, ring
estável`, "nosso lado impecável", logo o problema *só poderia* estar no transporte USB.

A correção do transporte (iso assíncrono, abaixo) foi implementada — e o novo motor
reportou janelas de **`OUT 1000 pkt/s (0 silent/5s)`**: 5 segundos inteiros com PCM real
em *todo* frame de 1ms, zero gaps, zero silêncio. **E continuou robótica.** Isso **refutou**
a teoria do timing: se fossem gaps, essas janelas teriam saído limpas. O artefato robótico
era **formato dos dados**, não pacing.

**Lição:** "pacing perfeito e ainda quebrado" ≠ "a próxima camada é o transporte". Quando o
timing está comprovadamente limpo e o áudio ainda é lixo, o problema é o **conteúdo/formato**
do que se manda, não *quando*. Foi o mesmo tipo de erro do capítulo "áudio impossível":
concluir causa raiz a partir de evidência ambígua sem comparar com o driver de referência.

### Causa raiz real: faltava o header GIP em cada pacote OUT

Comparando com o `gip_copy_audio_samples()` do **xone**, cada pacote de áudio OUT **não é
PCM cru** — é um frame **GIP `AUDIO_SAMPLES`**:

```
[0x60] [0x21 = client_id|INTERNAL] [seq: incrementa 1..255, nunca 0] [len varint = 192]  |  192B PCM
```

É o **mesmo enquadramento que o mic usa na entrada** (`60 21 <seq> <len> | … | PCM`), que a
gente já decodificava — só não tínhamos percebido a simetria. Mandando PCM cru, o firmware
tentava interpretar bytes de PCM como header/sequência, dessincronizava e **sintetizava lixo
(voz robótica) constante, independente do pacing**.

Detalhe histórico que confirma: o antigo prefixo `<u16 192>` (= `c0 00`) nunca foi o header
certo — faltava `60 21 <seq>`. Por isso "PCM cru" soou *menos pior* que o prefixo, mas nenhum
dos dois era correto. O header GIP completo `60 21 <seq> c0 81 00` nunca tinha sido testado.

### As duas mudanças (em `tools/iso_audio.py` + `gip_init.py`)

1. **Enquadramento GIP no OUT** (a correção): cada pacote iso OUT = header GIP de 6 bytes
   (`build_gip_header(0x60, 0x21, seq, 192)`) + 192B de PCM = **198B/pacote**, com **sequência
   incremental por pacote**. Espelha o xone byte a byte.
2. **Iso assíncrono via `python-libusb1` (`usb1`)** (higiene de transporte + a evidência):
   N transfers sempre em voo (OUT 6×8pkt ≈ 48ms, IN 4×8pkt), resubmetidos em callback,
   alimentando os **mesmos rings do shim C** (`wpw_read_playback`/`wpw_write_capture`).
   `usb1` reivindica **só a interface 1**; o `pyusb` mantém EP1 (GIP) e EP2 (bulk) — duas
   handles libusb no mesmo device, cada uma com interfaces diferentes. A negociação GIP
   (POWER/FORMAT) no EP1 acontece ANTES do `usb1` pegar a interface 1 — a ordem importa.
   Priming de ~40ms no ring antes de drenar áudio real, pra bursts do PipeWire não esvaziarem
   o ring no meio do stream.

Se o `usb1` não estiver disponível ou a interface 1 der `BUSY`, o `main` cai no caminho
síncrono antigo (`stream_audio_out`/`monitor_audio`) — que agora **também** enquadra o OUT
como GIP, então a diferença passa a ser só o pacing.

---

## ✅ RESOLVIDO — Buzz no canal esquerdo (era o mesmo bug de formato)

**Sumiu junto com o fix do enquadramento GIP no OUT.** A conclusão anterior — "é
analógico/hardware, não está no nosso sinal" — estava **errada**, pelo mesmo motivo da voz
robótica: era outro sintoma de mandar PCM cru onde o firmware espera frames GIP. Os bytes
mal-enquadrados (o antigo prefixo `c0 00` e o desalinhamento de meio-frame) vazavam como
amostras no canal esquerdo. Com cada pacote OUT enquadrado como `60 21 <seq> <len>` + PCM
alinhado, o buzz **desapareceu**.

### Por que as "provas de hardware" enganaram

| Prova de então | Interpretação correta |
|---|---|
| Com "zeros crus" o buzz continua | Não eram zeros *enquadrados*: sem o header GIP, o device interpretava lixo/desalinhamento como PCM → ruído mesmo com payload zerado |
| Escala com o botão físico de volume | O ruído já estava no sinal digital malformado; o amp analógico só o amplifica junto com o resto |
| Só no canal esquerdo | O desalinhamento de meio-frame (194B = 48,5 frames) trocava a paridade L/R → o erro caía sistematicamente num canal |

**Lição (a mesma da voz robótica):** antes de declarar "é hardware", garantir que o sinal
digital que a gente manda está no formato **exato** do driver de referência. Duas "limitações
de hardware irreversíveis" deste projeto (áudio impossível, depois o buzz) eram, na verdade,
o host mandando a coisa errada.

---

## Estado atual do código (arquivos novos desta fase)

- **`tools/wolverine_pw.c`** — shim PipeWire nativo (sink+source, rings). Compila limpo com
  PipeWire 1.6.6.
- **`tools/Makefile`** — `make -C tools` gera `wolverine_pw.so` (gitignored).
- **`tools/iso_audio.py`** — motor iso assíncrono do EP3 via `usb1` (classe `IsoAudio`):
  N transfers OUT/IN sempre em voo, callbacks realimentando os rings do shim C. OUT
  **GIP-framed** (header injetado do gip_init) + priming; IN parseia GIP e empurra pro source.
- **`tools/gip_init.py`** — integração: `load_pipewire_bridge()` (ctypes), instancia
  `IsoAudio` (caminho preferido); `stream_audio_out`/`monitor_audio` são o **fallback
  síncrono** (também GIP-framed) se o `usb1`/interface 1 falhar; `forward_media` (botões).
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
8. Reivindica interfaces [0,2] no pyusb (a 1 fica livre p/ o `usb1`); ativa alt=1 na interface 2
9. **Inicia o bridge PipeWire** (`load_pipewire_bridge` + `wpw_start`) → sink + source
10. **Sobe o motor iso assíncrono** (`IsoAudio`, `usb1` reivindica a interface 1 + alt=1):
    OUT GIP-framed sink→EP3 OUT, IN EP3→source. Fallback síncrono se `usb1`/iface 1 falhar.
    Monitora EP1 IN (GIP/gamepad) e EP2 IN (ctrl/bulk) no pyusb.
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
    ├── iso_audio.py        ← motor iso assíncrono do EP3 via usb1 (OUT GIP-framed + IN)
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
python-libusb1       # instalado (3.3.1) — EP3 iso assíncrono (módulo iso_audio.py)
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
