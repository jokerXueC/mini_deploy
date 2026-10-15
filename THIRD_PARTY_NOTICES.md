# Third-Party Notices

## xterm.js and Fit Addon

The terminal bundles @xterm/xterm 5.5.0 and @xterm/addon-fit 0.10.0 from npm.
Both use the MIT license. Complete notices are preserved in
ui/vendor/xterm.LICENSE and ui/vendor/addon-fit.LICENSE.

Registry SHA-512 integrity values:

- @xterm/xterm: hqJHYaQb5OptNunnyAnkHyM8aCjZ1MEIDTQu1iIbbTD/xops91NB5yq1ZK/dC2JDbVWtF23zUtl9JE2NqwT87A==
- @xterm/addon-fit: UFYkDm4HUahf2lnEyHvio51TNGiLK66mqP2JoATy7hRZeXaGMRDr00JiSF7m63vR5WKATF605yEggJKsw0JpMQ==

## Paramiko

SSH transport uses Paramiko (LGPL-2.1-or-later), installed separately by the
Linux package manager, or using requirements-ssh.txt for development.
https://www.paramiko.org/

## Markdown Rendering

AI replies bundle Marked 18.1.0 (MIT) and DOMPurify 3.4.16
(Apache-2.0 OR MPL-2.0) from npm. Full notices are preserved in
ui/vendor/marked.LICENSE, ui/vendor/purify.LICENSE and ui/vendor/purify.LICENSE-MPL.
Both are served locally; no external CDN is used at runtime.

Registry SHA-512 integrity values verified when vendoring:

- marked: PamYXWWWg2nboG3oX5Ffzpy3EFZROqZJK9hSlMmUnekp7TxPs5DcT95vwd46m73/exfKCRVMtgXoPMI4FEJL6w==
- dompurify: sqo+pNp3qRhCIpbgRi1y8Tgk27Bo2Ry7w0dC1NBeNTdZChWjz9Xb/KOoZbRP/R6pQZ80Qw8YhXw13hWWBbMRnQ==

## Lucide Icons

The public website bundles Lucide SVGs in website/assets/. Its license is
preserved in website/assets/LUCIDE-LICENSE. Fail2ban is installed through the
host package manager when requested; no Fail2ban or SafeLine source is bundled.

The refresh-cw and ellipsis SVG icons in ui/index.html are from lucide-static
v0.468.0 (https://lucide.dev). The SSH AI design preview also bundles SVGs from
this version in ui/previews/ssh-ai/icons/, with the full license in that directory.

ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
