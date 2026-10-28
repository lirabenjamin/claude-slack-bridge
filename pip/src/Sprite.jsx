// 8-bit pixel-art sprites. Each kind is a 16x16 grid of palette codes.
// Palette codes:
//   .  transparent
//   B  body main          (hsl from hue)
//   S  body shadow        (darker hue)
//   H  body highlight     (lighter hue)
//   D  outline / eye dark (#1a1722)
//   W  eye shine          (white)
//   Y  accent yellow      (beak / mushroom spot)
//   P  pink cheek         (complementary hue)

const PIXEL = 4 // each grid cell is 4x4 SVG units → 64×64 viewBox
const N = 16

const palette = (hue) => ({
  '.': null,
  B: `hsl(${hue} 70% 65%)`,
  S: `hsl(${hue} 55% 38%)`,
  H: `hsl(${hue} 90% 85%)`,
  D: '#1a1722',
  W: '#ffffff',
  Y: '#ffae42',
  P: `hsl(${(hue + 320) % 360} 80% 78%)`,
})

const GRIDS = {
  blob: [
    '................',
    '.....BBBBBB.....',
    '...BBHHBBBBBB...',
    '..BHHBBBBBBBBB..',
    '.BHBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    'BBBBDDBBBBDDBBBB',
    'BBBBDWBBBBDWBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBDBBBBDBBBBB',
    'BBBBBBDDDDBBBBBB',
    '.BBBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    '..SSBBBBBBBBSS..',
    '....SSSSSSSS....',
    '................',
  ],
  ghost: [
    '................',
    '.....BBBBBB.....',
    '...BBHHBBBBBB...',
    '..BHHBBBBBBBBB..',
    '.BHBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    'BBBBDDBBBBDDBBBB',
    'BBBBDWBBBBDWBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBBDDDDBBBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBB.BBBB.BBBB.BB',
    'BB...BBB...BB..B',
    'B....BBB...BB...',
  ],
  cat: [
    '................',
    '...B........B...',
    '..BB........BB..',
    '.BBBB......BBBB.',
    '.BBBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    '.BBDDBBBBBBDDBB.',
    '.BBDWBBBBBBDWBB.',
    'BBBBBBBYYBBBBBBB',
    'BBBBBBDDDDBBBBBB',
    'BBBBBBBBBBBBBBBB',
    '.BBBBBBBBBBBBBB.',
    '..BBBBBBBBBBBB..',
    '...BBBBBBBBBB...',
    '....BBBBBBBB....',
    '....SS....SS....',
  ],
  mushroom: [
    '................',
    '.....BBBBBB.....',
    '...BBBHBBBBBB...',
    '..BBHHBBBBBBBB..',
    '.BBHWBBBBBBBBBB.',
    'BBBBBBBWBBBWBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBBBWBBBBBBBB',
    'SSSSSSSSSSSSSSSS',
    '....HHHHHHHH....',
    '....HHDDHHDDH...',
    '....HHWHHHWHH...',
    '....HHHHHHHHH...',
    '....HHHDDDDHH...',
    '....HHHHHHHHH...',
    '....SSSSSSSSS...',
  ],
  bird: [
    '................',
    '.....BBBBBB.....',
    '...BBHHBBBBBB...',
    '..BHHBBBBBBBBB..',
    '.BHBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    'BBBBDDBBBBDDBBBB',
    'BBBBDWBBBBDWBBBB',
    'BBBBBBBYYYBBBBBB',
    'BBBBBBYYYYYBBBBB',
    'BBBBBBBYYYBBBBBB',
    '.BBBBBBBBBBBBBB.',
    '.BBBBBBBBBBBBBB.',
    '..BBBBBBBBBBBB..',
    '....YY....YY....',
    '...YYYY..YYYY...',
  ],
  slime: [
    '................',
    '................',
    '......BBBB......',
    '....BHHBBBBB....',
    '...BHBBBBBBBB...',
    '..BBBBBBBBBBBB..',
    '.BBBBDDBBBBDDBB.',
    '.BBBBDWBBBBDWBB.',
    '.BBBBBBBBBBBBBB.',
    'BBBBBDDDDDDBBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBBBBBBBBBBBBBBB',
    'BBB.BBB.BBB.BBBB',
    'BB...BB...BB...B',
    'B....B.....B....',
    '................',
  ],
}

// Optional accessory layer per kind: small bits drawn over the base grid.
// For now, none. Future: hats, blush, etc.

export default function Sprite({ kind, hue }) {
  const pal = palette(hue)
  const grid = GRIDS[kind] || GRIDS.blob
  const rects = []
  for (let y = 0; y < N; y++) {
    const row = grid[y]
    if (!row) continue
    let runStart = -1
    let runColor = null
    const flush = (xEnd) => {
      if (runStart >= 0 && runColor) {
        rects.push(
          <rect
            key={`${y}-${runStart}-${xEnd}`}
            x={runStart * PIXEL}
            y={y * PIXEL}
            width={(xEnd - runStart) * PIXEL}
            height={PIXEL}
            fill={runColor}
          />,
        )
      }
      runStart = -1
      runColor = null
    }
    for (let x = 0; x < N; x++) {
      const c = row[x]
      const fill = pal[c]
      if (fill === runColor) continue
      flush(x)
      if (fill) {
        runStart = x
        runColor = fill
      }
    }
    flush(N)
  }
  return (
    <svg
      viewBox={`0 0 ${N * PIXEL} ${N * PIXEL}`}
      className="sprite-svg"
      shapeRendering="crispEdges"
      xmlns="http://www.w3.org/2000/svg"
    >
      {rects}
    </svg>
  )
}
