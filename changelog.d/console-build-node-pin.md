### Changed

- **The admin console is built with one exact Node, so its packaged `.gz` and `.br` files are the same bytes on every machine** (console-build-node-pin). The compressed assets come from the zlib and brotli inside Node, and two Node builds wrote different bytes under the same file names. `admin/.nvmrc`, `engines.node` and the CI `setup-node` steps now name Node 22.21.1, and `npm run package` refuses any other Node. A plain `npm run build` only warns. The committed console is unchanged: Node 22.21.1 reproduces its bytes. No upgrade step.
