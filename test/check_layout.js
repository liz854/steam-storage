// 布局几何校验：块数 / 越界 / 重叠 / 卡片比例 / 大小顺序 / 画布利用率
// 唯一布局是卡片流（layoutFlow），卡片长宽比锁死封面比例 460:215
const fs = require('fs');
const path = require('path');

const dir = path.join(__dirname, '..');
const tpl = fs.readFileSync(path.join(dir, 'web', 'app.js'), 'utf8');

const start = tpl.indexOf('var COVER_AR = TARGET_AR;');
const end = tpl.indexOf('function renderMap(');
if (start < 0 || end < 0) throw new Error('找不到布局函数块');

const arDef = tpl.match(/var TARGET_AR = [^;]+;/);
if (!arDef) throw new Error('找不到 TARGET_AR 常量');

const layoutFlow = new Function(
  arDef[0] + '\n' + tpl.slice(start, end) + '\n; return layoutFlow;'
)();
const AR = 460 / 215;

let pass = 0, fail = 0;
function ok(cond, label, detail) {
  if (cond) { pass++; console.log('PASS  ' + label.padEnd(28) + ' ' + detail); }
  else { fail++; console.log('FAIL  ' + label.padEnd(28) + ' ' + detail); process.exitCode = 1; }
}

function check(W, H, items, label) {
  const lay = layoutFlow(items, W, H);
  const r = lay.rects;
  const tag = label.padEnd(22);

  ok(r.length === items.length, tag + '块数', r.length + '/' + items.length);
  if (r.length !== items.length) return;

  // 画布高度是跟着内容走的，越界上限按它自己报的高度算
  const maxH = Math.max(H, lay.height) + 0.6;

  let oob = 0, ov = 0, arMax = 0;
  for (const x of r) {
    if (x.x < -0.5 || x.y < -0.5 || x.x + x.w > W + 0.6 || x.y + x.h > maxH) oob++;
    const a = x.w / x.h;
    arMax = Math.max(arMax, Math.max(a / AR, AR / a));
  }
  for (let i = 0; i < r.length; i++) {
    for (let j = i + 1; j < r.length; j++) {
      const A = r[i], B = r[j];
      const ox = Math.min(A.x + A.w, B.x + B.w) - Math.max(A.x, B.x);
      const oy = Math.min(A.y + A.h, B.y + B.h) - Math.max(A.y, B.y);
      if (ox > 0.5 && oy > 0.5) ov++;
    }
  }

  const area = r.reduce((s, x) => s + x.w * x.h, 0);
  const maxY = Math.max(...r.map(x => x.y + x.h));
  const fill = area / (W * Math.max(maxY, 1));

  // 大小顺序不能乱：占用更大的游戏，块不能比占用小的还小
  let orderBad = 0;
  for (let i = 0; i < r.length; i++) {
    for (let j = i + 1; j < r.length; j++) {
      const ai = r[i].w * r[i].h, aj = r[j].w * r[j].h;
      const si = r[i].g.size, sj = r[j].g.size;
      if (!si || !sj) continue;
      const bigger = si > sj ? ai / aj : aj / ai;
      if (bigger < 0.75) orderBad++;
    }
  }

  ok(oob === 0, tag + '不越界', '越界 ' + oob);
  ok(ov === 0, tag + '不重叠', '重叠 ' + ov);
  // 长宽比必须严格等于封面比例，这是"封面不裁切也不留白"的前提
  ok(arMax < 1.02, tag + '比例=封面比例', '最大偏离 ' + arMax.toFixed(3));
  ok(orderBad === 0, tag + '大小顺序不乱', '逆序 ' + orderBad + ' 对');
  ok(fill > 0.48, tag + '画布利用率', (fill * 100).toFixed(0) + '%');
}

const data = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixture.json'), 'utf8'));
const games = data.games.slice().sort((a, b) => b.size - a.size);

const cases = [
  [1200, 600, games, '全部 ' + games.length + ' 个 1200x600'],
  [800, 420, games, '全部 ' + games.length + ' 个 800x420'],
  [400, 900, games, '全部 ' + games.length + ' 个 窄屏'],
  [1200, 600, games.slice(0, 1), '单个游戏'],
  [1200, 600, games.slice(0, 3), '前 3 个'],
  [1200, 600, games.slice(0, 12), '前 12 个'],
  [1200, 600, [{ size: 100 }, { size: 1 }, { size: 1 }], '极端悬殊比例'],
  [300, 260, games, '极小画布'],
];

for (const [W, H, items, label] of cases) check(W, H, items, label);

console.log('\n' + pass + ' 项通过' + (fail ? '，' + fail + ' 项失败' : ''));
