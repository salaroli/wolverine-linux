# wolverine-linux

Driver em userspace para o Razer Wolverine V2 Chroma no Linux: gamepad, áudio do jack (sink e source PipeWire), botões de mídia e rumble.
O produto é um binário Rust único, `wolverined` (`rust/`), instalado como daemon systemd ativado por udev (`packaging/`). `tools/` (Python e
um shim em C) é **referência/legado**, não o produto.

## Estado (2026-07-06, conferido no `CONTEXT.md` e no `git log` em 2026-10-25)

Feature-complete e validado em hardware real; PRs #1–#5 mergeados na `main` (o #5 fecha a latência de áudio). **Não há trabalho pendente
conhecido.** Antes de afirmar estado, confira o `git log` e a seção "Status" do `README.md`.

## Regras

- **Branch por feature + PR para a `main`; nunca commit na `main`** (preferência do dono). *Conventional commits*.
- **Depois de mergear mudança no driver, o daemon instalado continua o binário antigo.** Reinstale: `git checkout main && git pull`,
  `cd rust && cargo build --release`, `sudo cp target/release/wolverined /usr/local/bin/ && sudo systemctl restart wolverined`
  (ou `sudo ./packaging/install.sh`). Mudança só de docs não precisa.
- **Validação é em hardware**: o dono confirma por ouvido/uso. Meça o sintoma antes de afinar (latência tem duas causas ortogonais: piso, em
  ms, e teto, ring ilimitado, em segundos).
- O driver roda como root, **detacha o `xpad`** e o reatacha ao sair; não coexiste com ele.

## Mapa

| Onde | O quê |
|---|---|
| `README.md` | status, uso, variáveis de ambiente (`WOLVERINE_*`), instalação como serviço |
| `CONTEXT.md` | **fonte de contexto**: protocolo GIP, mapa USB, decisões do rewrite em Rust, causas-raiz de cada problema resolvido |
| `docs/usb-analysis.md` | análise dos descritores USB |
| `rust/src/` | `wolverined` (`iso.rs` motor isócrono EP3 via `libusb1-sys`; `audio.rs` PipeWire nativo, `pipewire-rs` 0.10 com feature `v0_3_49`) |
| `packaging/` | `install.sh`, unit systemd, regra udev |

## Conhecimento do projeto

Tudo que já foi aprendido está no [`CONTEXT.md`](CONTEXT.md): não re-derive, em especial (a) "Decisões técnicas que custaram investigação"
(nusb sem API isócrona, `pipewire-rs` 0.10 e não 0.8 contra o PipeWire 1.6.6, mapa do gamepad no layout do `xpad`), e (b) "CAUSA RAIZ:
ring ilimitado" e a regressão do mic cortado (ouça `pw_buffer.requested`). Estado, decisão ou armadilha novos entram no `CONTEXT.md` ou no
`README.md` **na mesma branch e PR da mudança**, sem credenciais, separando medido de suposto. Memória externa do Claude não é o lugar.
