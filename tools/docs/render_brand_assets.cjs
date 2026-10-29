/* Maintenance only. Requires Sharp 0.35.5 and Python 3 for the deterministic ZIP. */
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const sharp = require('sharp');
const repo = path.resolve(__dirname, '../..');
const assets = path.join(repo, 'website/assets');
const brand = path.join(assets, 'brand');
const read = name => fs.readFileSync(path.join(brand, name), 'utf8').match(/<svg[^>]*>([\s\S]*)<\/svg>/)[1];
function tile(size) {
  const small = size < 24;
  const side = small ? 16 : 80;
  const body = read(small ? 'sandloom-mark-micro.svg' : 'sandloom-mark.svg');
  return Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${side} ${side}"><rect width="${side}" height="${side}" rx="${side * .18}" fill="#111213"/><g fill="#d8b892">${body}</g></svg>`);
}
(async () => {
  for (const color of ['ink', 'sand', 'white']) {
    await sharp(path.join(brand, `sandloom-mark-${color}.svg`)).resize(1024,1024).png().toFile(path.join(brand, `sandloom-mark-${color}.png`));
    await sharp(path.join(brand, `sandloom-lockup-${color}.svg`)).resize({width:1600}).png().toFile(path.join(brand, `sandloom-lockup-${color}.png`));
  }
  for (const size of [16,20,24,32,48,64,96]) {
    await sharp(tile(size)).png().toFile(path.join(brand, `favicon-${size}.png`));
  }
  await sharp(path.join(brand, 'sandloom-avatar.svg')).resize(1024,1024).png().toFile(path.join(brand, 'sandloom-avatar.png'));
  await sharp(path.join(brand, 'sandloom-avatar.svg')).resize(180,180).png().toFile(path.join(brand, 'apple-touch-icon.png'));
  fs.copyFileSync(path.join(assets, 'favicon.svg'), path.join(brand, 'favicon.svg'));
  fs.copyFileSync(path.join(brand, 'favicon-96.png'), path.join(assets, 'favicon-96.png'));
  fs.copyFileSync(path.join(brand, 'apple-touch-icon.png'), path.join(repo, 'website/apple-touch-icon.png'));
  const sizes = [16,32,48];
  const frames = await Promise.all(sizes.map(size => sharp(tile(size)).png().toBuffer()));
  const header = Buffer.alloc(6 + sizes.length * 16);
  header.writeUInt16LE(1, 2); header.writeUInt16LE(sizes.length, 4);
  let offset = header.length;
  sizes.forEach((size, i) => {
    const at = 6 + i * 16;
    header[at] = size; header[at + 1] = size;
    header.writeUInt16LE(1, at + 4); header.writeUInt16LE(32, at + 6);
    header.writeUInt32LE(frames[i].length, at + 8); header.writeUInt32LE(offset, at + 12);
    offset += frames[i].length;
  });
  const ico = Buffer.concat([header, ...frames]);
  fs.writeFileSync(path.join(repo, 'website/favicon.ico'), ico);
  fs.writeFileSync(path.join(brand, 'favicon.ico'), ico);
  for (const kind of ['home', 'docs', 'examples']) {
    const stem = path.join(assets, `social/sandloom-${kind}`);
    await sharp(`${stem}.svg`).png().toFile(`${stem}.png`);
  }
  await sharp(path.join(assets, 'social/sandloom-home.svg')).resize(1280,640,{fit:'contain',background:'#111213'}).png().toFile(path.join(brand, 'github-social-preview.png'));
  execFileSync('python3', ['-c', `
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
folder = Path('website/assets/brand')
with ZipFile(folder / 'sandloom-brand-kit.zip', 'w', ZIP_DEFLATED, compresslevel=9) as archive:
    for item in sorted(folder.iterdir()):
        if item.suffix == '.zip':
            continue
        info = ZipInfo('Sandloom/' + item.name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, item.read_bytes())
`], {cwd:repo, stdio:'inherit'});
  console.log('Prepared caracal PNG/ICO, three sharing cards and deterministic brand kit.');
})();
