// 在 Node 里用极简 DOM stub 实际执行一次 renderReport，抓运行时错误
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const dir = path.join(__dirname, '..');

class El {
  constructor(sel) {
    this.sel = sel;
    this.tagName = 'DIV';
    this.innerHTML = '';
    this.textContent = '';
    this.style = {};
    this.dataset = {};
    this.value = '';
    this.clientWidth = 1200;
    this.clientHeight = 600;
    const set = new Set();
    this.classList = {
      add: (...a) => a.forEach(x => set.add(x)),
      remove: (...a) => a.forEach(x => set.delete(x)),
      contains: x => set.has(x),
    };
  }
  addEventListener(type, fn) {
    if (!this._h) this._h = {};
    (this._h[type] = this._h[type] || []).push(fn);
  }
  fire(type, evt) {
    ((this._h || {})[type] || []).forEach(fn => fn(evt));
  }
  removeEventListener() {}
  setAttribute() {}
  scrollIntoView() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { width: 220, height: 90 }; }
}

const cache = new Map();
const getEl = sel => {
  if (!cache.has(sel)) cache.set(sel, new El(sel));
  return cache.get(sel);
};

const sandbox = {
  window: {},
  document: {
    readyState: 'complete',
    querySelector: getEl,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  innerWidth: 1440,
  innerHeight: 900,
  addEventListener: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
  Date, Math, Map, Set, JSON, Number, String, Array, Object, Intl, console,
  parseInt, parseFloat, isNaN,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(dir, 'web', 'app.js'), 'utf8'), ctx);

// 合成数据，不依赖本机 Steam（CI 可跑）
const data = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixture.json'), 'utf8'));

let failed = 0;
function assert(cond, msg) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + msg);
  if (!cond) failed++;
}

try {
  ctx.renderReport(data);
} catch (e) {
  console.log('FAIL  renderReport 抛异常: ' + e.message);
  console.log(e.stack.split('\n').slice(0, 4).join('\n'));
  process.exit(1);
}

const stats = getEl('#stats').innerHTML;
const treemap = getEl('#treemap').innerHTML;
const tbody = getEl('#tbody').innerHTML;
const rank = getEl('#rank').innerHTML;
const subline = getEl('#subline').innerHTML;
const foot = getEl('#foot').innerHTML;

const GB = 1024 ** 3;
const totalStr = (data.totals.bytes / GB).toFixed(1) + ' GB';
const biggest = data.games.slice().sort((a, b) => b.size - a.size)[0];
const biggestStr = (biggest.size / GB).toFixed(1) + ' GB';
const longest = data.games.slice().sort((a, b) => (b.playtime || 0) - (a.playtime || 0))[0];
const totalHours = Math.round(data.totals.playtimeMinutes / 60);

assert(stats.includes(totalStr), `统计卡包含总占用 ${totalStr}`);
assert(stats.includes(String(data.games.length)), `统计卡包含游戏数 ${data.games.length}`);
assert(stats.includes('累计游玩'), '统计卡有「累计游玩」');
assert(stats.includes(totalHours.toLocaleString()) || stats.includes(String(totalHours)),
  `统计卡显示总时长 ${totalHours} 小时`);
assert(stats.includes(longest.name), '统计卡显示玩得最久的游戏');
assert(subline.includes('appmanifest'), '副标题指向数据源');
assert(foot.includes('SizeOnDisk'), '页脚标注数据字段');

const tileCount = (treemap.match(/class="tile"/g) || []).length;
assert(tileCount === data.games.length, `treemap 卡片数 ${tileCount} == 游戏数 ${data.games.length}`);
assert(treemap.includes('width:') && treemap.includes('background:hsl'), 'treemap 输出尺寸与颜色');

const rowCount = (tbody.match(/<tr /g) || []).length;
assert(rowCount === data.games.length, `表格行数 ${rowCount}`);
assert(tbody.includes(biggestStr), `表格显示最大游戏占用 ${biggestStr}`);
assert(tbody.includes('小时') || tbody.includes('分钟'), '表格显示游玩时长');
assert(tbody.includes('libtag'), '表格带库标签');

const rankRows = (rank.match(/class="rrow"/g) || []).length;
assert(rankRows === Math.min(14, data.games.length), `排行榜 ${rankRows} 行`);

/* ---- 切到「按时长」视图 ---- */
const chip = { dataset: { metric: 'playtime' }, classList: { add() {}, remove() {} } };
getEl('#metricChips').fire('click', { target: { closest: () => chip } });

const mapTitle = getEl('#mapTitle').textContent;
assert(mapTitle.includes('游玩时长'), '切到时长视图后标题变化：' + mapTitle);

const plTiles = (getEl('#treemap').innerHTML.match(/class="tile"/g) || []).length;
const withTime = data.games.filter(g => (g.playtime || 0) > 0).length;
assert(plTiles === withTime, `时长视图卡片数 ${plTiles} == 有时长的游戏数 ${withTime}`);

const plRank = getEl('#rank').innerHTML;
assert(plRank.indexOf('Counter-Strike 2') < plRank.indexOf('ELDEN RING'),
  '时长排行里 CS2 排在 ELDEN RING 前面');
assert(getEl('#rankTitle').textContent.includes('游玩时长'), '排行标题跟着变');

/* ---- 切回按存储 ---- */
const chip2 = { dataset: { metric: 'size' }, classList: { add() {}, remove() {} } };
getEl('#metricChips').fire('click', { target: { closest: () => chip2 } });
assert(getEl('#mapTitle').textContent.includes('磁盘占用'),
  '切回存储视图：' + getEl('#mapTitle').textContent);

// 空数据保护
try {
  ctx.renderReport({ games: [], libraries: [], totals: {} });
  assert(false, '空数据应当抛错');
} catch (e) {
  assert(true, '空数据抛错符合预期');
}

// 极小数据（单个游戏）不应崩
try {
  ctx.renderReport({
    generatedAt: 'x', steamRoot: 'x',
    libraries: [{ path: 'L', total: null, free: null, gameCount: 1, gameBytes: 1024 }],
    games: [{ appid: 1, name: 'Solo', size: 1024, lastPlayed: null, lib: 'L' }],
    totals: { bytes: 1024, count: 1, libraryCount: 1 },
  });
  assert(getEl('#treemap').innerHTML.includes('class="tile"'), '单个游戏也能渲染');
} catch (e) {
  assert(false, '单个游戏渲染崩溃: ' + e.message);
}

/* ---- 导出的单文件报告：预置数据直接启动 ---- */
function freshEnv(reportData) {
  const c2 = new Map();
  const get = sel => {
    if (!c2.has(sel)) c2.set(sel, new El(sel));
    return c2.get(sel);
  };
  const sb = {
    document: {
      readyState: 'complete',
      querySelector: get,
      querySelectorAll: () => [],
      addEventListener: () => {},
    },
    innerWidth: 1440,
    innerHeight: 900,
    addEventListener: () => {},
    setTimeout: () => 0,
    clearTimeout: () => {},
    Date, Math, Map, Set, JSON, Number, String, Array, Object, Intl, console,
    parseInt, parseFloat, isNaN,
    // 预置数据时不该发任何网络请求，发了就说明走错了分支
    fetch: () => { throw new Error('预置数据模式下不应发起网络请求'); },
    __REPORT_DATA__: reportData,
  };
  sb.window = sb;
  sb.globalThis = sb;
  const c = vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(dir, 'web', 'app.js'), 'utf8'), c);
  return { ctx: c, get };
}

try {
  const env2 = freshEnv(JSON.parse(JSON.stringify(data)));
  const t2 = (env2.get('#treemap').innerHTML.match(/class="tile"/g) || []).length;
  assert(t2 === data.games.length, `预置数据直接出图（${t2} 个卡片），跳过选择面板`);
  assert(env2.get('#stats').innerHTML.includes(totalStr), '预置数据渲染出统计卡');
  assert(env2.get('#tbody').innerHTML.includes('<tr '), '预置数据渲染出表格');
} catch (e) {
  assert(false, '预置数据启动失败: ' + e.message);
}

/* ---- 封面：够大的卡片铺图，太小的保持纯色 ---- */
{
  const big = JSON.parse(JSON.stringify(data));
  big.games[0].cover = 'data:image/jpeg;base64,AAAA';
  const env3 = freshEnv(big);
  const tm = env3.get('#treemap').innerHTML;

  assert((tm.match(/has-cover/g) || []).length === 1, '只有设了封面的卡片加 has-cover 类');
  assert(tm.includes('background-color:'), '铺图时保留底色，图挂了自动露出');

  // 关键回归：外层 style 属性用双引号，url() 里面必须用单引号。
  // 用双引号的话浏览器在 url(" 处就把属性截断了，整串 base64 变成垃圾属性，封面根本不显示。
  assert(tm.includes("--cv:url('data:image/jpeg;base64,AAAA')"),
    'url() 用单引号包裹，不与外层 style 的双引号冲突');
  assert(!tm.includes('url("'), '不允许出现 url(" 的写法');
  assert((tm.match(/<i class="cv"><\/i>/g) || []).length === 1,
    '有封面的卡片挂一层独立的主体图元素（虚化底在它下面）');

  // 按浏览器的方式解析：style="..." 必须能完整取到，且里面 url(...) 闭合
  const tiles = [...tm.matchAll(/<div class="tile[^"]*" data-id="\d+" style="([^"]*)">/g)];
  assert(tiles.length === data.games.length,
    `每个卡片都能被解析出 style 属性（${tiles.length}/${data.games.length}）`);

  const withCover = tiles.filter(t => t[1].includes('--cv:'));
  assert(withCover.length === 1, `解析出的 style 里只有 1 个带封面图，实际 ${withCover.length}`);

  const s = withCover[0][1];
  assert(/--cv:url\('[^']*'\)/.test(s), 'style 属性内的 url(...) 完整闭合');
  assert(s.includes('base64,AAAA'), 'style 属性里保留了完整的图片数据，没被截断');
  assert(/width:[\d.]+px/.test(s) && /height:[\d.]+px/.test(s), 'style 属性里的宽高也完好');
  // 铺封面的卡片底色必须是暗版，否则等比缩放剩下的空隙会亮得刺眼
  const bg = s.match(/background-color:hsl\(\d+ \d+% (\d+)%\)/);
  assert(bg && Number(bg[1]) < 40, '铺封面的卡片用暗色底');
  // 同一张图只写一次：主体图和虚化底共用 --cv，报告体积不会翻倍
  assert(s.indexOf('base64,AAAA') === s.lastIndexOf('base64,AAAA'),
    '图片数据在 style 里只出现一次');
}

/* ---- 封面显示方式：主体图必须 contain，一旦退回 cover 就会裁掉两边 ---- */
{
  const css = fs.readFileSync(path.join(dir, 'web', 'style.css'), 'utf8');
  const rule = (sel) => {
    const esc = sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const m = css.match(new RegExp(esc + '\\{([^}]*)\\}'));
    return m ? m[1] : '';
  };
  const main = rule('.tile.has-cover .cv');
  assert(/background-size:\s*contain/.test(main), '主体封面用 contain，整张图完整显示');
  assert(!/background-size:\s*cover/.test(main),
    '主体封面不能退回 cover（那会把 460×215 的封面切掉两边）');
  assert(/background-position:\s*center/.test(main), '主体封面居中');

  // 虚化底已经拿掉：留白露出卡片自己的暗色底，糊一层反而更像"重影"
  assert(!rule('.tile.has-cover::before'), '不再用虚化底填缝');
}

{
  // 最小的游戏在 1200x600 上只有几个像素，铺图只会糊成一片
  const tiny = JSON.parse(JSON.stringify(data));
  tiny.games.sort((a, b) => a.size - b.size);
  tiny.games[0].cover = 'data:image/jpeg;base64,BBBB';
  const tm = freshEnv(tiny).get('#treemap').innerHTML;
  assert(!tm.includes('has-cover'), '卡片太小时不铺封面');
}

{
  // 完全不设封面时，退化成原来的纯色块
  const none = JSON.parse(JSON.stringify(data));
  none.games.forEach(g => { delete g.cover; });
  const tm = freshEnv(none).get('#treemap').innerHTML;
  assert(!tm.includes('has-cover') && tm.includes('background:hsl'), '没有封面时退回纯色块');
}

console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
