# Third-party notices

Joro Awake's own code is MIT licensed (see `LICENSE`). The installer downloads
the following third-party packages at install time into a private virtual
environment at `%LOCALAPPDATA%\Joro\keep-awake\.venv`. They are **not** included
in the Joro Awake source ZIP, and they are not covered by Joro Awake's MIT
license.

| Package | Version constraint | License | Verbatim text |
|---|---|---|---|
| [pystray](https://github.com/moses-palmer/pystray) | 0.19.5 (pinned) | LGPLv3 (`License: LGPLv3`) | `licenses/pystray-COPYING.LGPL`, plus `licenses/pystray-COPYING` (GPLv3) |
| [Pillow](https://python-pillow.org/) | >=10.1,<13 (12.3.0 used in release testing) | MIT-CMU, with bundled-component notices | `licenses/Pillow-LICENSE` |
| [six](https://github.com/benjaminp/six) | required by pystray (1.17.0 used in release testing) | MIT | `licenses/six-LICENSE` |

Pillow's license file also contains the notices for the libraries it bundles in
its Windows wheels: brotli, freetype, harfbuzz, lcms2, libavif, libjpeg-turbo,
libpng, libwebp, openjpeg, tiff, xz and zlib-ng. Those notices are part of the
verbatim Pillow license text.

pystray is installed as a separate, replaceable package inside the private
virtual environment rather than copied into Joro Awake's own source, and its
license text is shipped here. If you modify pystray for your own use, comply
with LGPLv3 for that copy.

The license texts in `licenses/` were copied verbatim from the packages' own
distribution metadata (`pystray-0.19.5.dist-info`, `pillow-12.3.0.dist-info`,
`six-1.17.0.dist-info`). Joro Awake's MIT text is the standard MIT license text
from the SPDX license list.
