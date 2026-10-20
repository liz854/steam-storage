// 零依赖路径的端到端校验：
// 模拟用户在不装 Python 的机器上，用浏览器选一个文件夹，页面自己解析出结果。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const dir = path.join(__dirname, '..');
const GB = 1024 ** 3;
const APP = fs.readFileSync(path.join(dir, 'web', 'app.js'), 'utf8');

class El {
  constructor(sel) {
    this.sel = sel;
    this.tagName = 'DIV';
    this.innerHTML = '';
    this.textContent = '';
    this.style = {};
    this.dataset = {};
    this.value = '';
    this.hidden = false;
    this.clientWidth = 1200;
    this.clientHeight = 600;
    this.files = null;
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
    ((this._h || {})[type] || []).forEach(fn => fn(evt || {}));
  }
  removeEventListener() {}
  setAttribute() {}
  scrollIntoView() {}
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  getBoundingClientRect() { return { width: 220, height: 90 }; }
}

// 每次调用拿到一套全新的页面状态，避免上一段的数据串进来
function makeEnv() {
  const cache = new Map();
  const get = sel => {
    if (!cache.has(sel)) cache.set(sel, new El(sel));
    return cache.get(sel);
  };

  const sandbox = {
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
    // 没有本地服务：请求一律失败，逼页面走纯静态分支
    fetch: () => Promise.reject(new Error('no local service')),
    // 封面会被转成 blob URL，这里给个可断言的替身
    URL: { createObjectURL: f => 'blob:fake/' + (f && f.name) },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  get.sandbox = sandbox;
  vm.runInContext(APP, vm.createContext(sandbox));
  return get;
}

let failed = 0;
function assert(cond, msg) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + msg);
  if (!cond) failed++;
}

const acf = (appid, name, bytes) => `"AppState"
{
\t"appid"\t\t"${appid}"
\t"name"\t\t"${name}"
\t"SizeOnDisk"\t\t"${bytes}"
\t"LastPlayed"\t\t"1750000000"
\t"installdir"\t\t"${name}"
}
`;

const localconfig = `"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"730"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"29640"
\t\t\t\t\t}
\t\t\t\t\t"570"
\t\t\t\t\t{
\t\t\t\t\t\t"Playtime"\t\t"3120"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
`;

function fakeFile(name, rel, content) {
  return { name, webkitRelativePath: rel, text: () => Promise.resolve(content) };
}

const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  /* 1) 选中 Steam 根目录：清单 + 时长都要解析出来 */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_730.acf', 'Steam/steamapps/appmanifest_730.acf',
        acf(730, 'Counter-Strike 2', Math.round(68.8 * GB))),
      fakeFile('appmanifest_570.acf', 'Steam/steamapps/appmanifest_570.acf',
        acf(570, 'Dota 2', Math.round(42.5 * GB))),
      fakeFile('localconfig.vdf', 'Steam/userdata/123/config/localconfig.vdf', localconfig),
    ];
    inp.fire('change');
    await wait(80);

    const treemap = get('#treemap').innerHTML;
    const tiles = (treemap.match(/class="tile[\s"]/g) || []).length;
    assert(tiles === 2, `选出 2 个游戏（实际 ${tiles} 个卡片）`);
    assert(treemap.includes('Counter-Strike 2'), '页面显示游戏名');
    assert(get('#tbody').innerHTML.includes('68.8 GB'), '表格显示占用大小');
    assert(get('#tbody').innerHTML.includes('494 小时'), '从 localconfig.vdf 读到游玩时长');
    assert(get('#subline').innerHTML.includes('Steam'), '顶部说明数据来源');
    assert(get('#view').classList.contains('show'), '结果区已展开');
  }

  /* 2) 选了个没有清单的文件夹：要明确报错，不能静默 */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [fakeFile('readme.txt', 'Downloads/readme.txt', 'hello')];
    inp.fire('change');
    await wait(30);

    const err = get('#err');
    assert(err.hidden === false, '空文件夹会提示错误');
    assert(String(err.textContent).includes('appmanifest'), '错误提示指向缺失的清单文件');
  }

  /* 3) 游戏目录里的同名 localconfig.vdf 不能被当成时长来源 */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_730.acf', 'Steam/steamapps/appmanifest_730.acf',
        acf(730, 'Counter-Strike 2', Math.round(68.8 * GB))),
      fakeFile('localconfig.vdf', 'Steam/steamapps/common/SomeGame/config/localconfig.vdf', localconfig),
    ];
    inp.fire('change');
    await wait(80);

    const tbody = get('#tbody').innerHTML;
    assert(tbody.includes('Counter-Strike 2'), '清单本身仍被读到');
    assert(!tbody.includes('494'), '游戏目录下的同名文件被忽略，时长保持为空');
  }

  /* 4) 只含附加库（没有 userdata）：能出图，时长为空 */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_1174180.acf', 'SteamLibrary/steamapps/appmanifest_1174180.acf',
        acf(1174180, 'Red Dead Redemption 2', Math.round(119.5 * GB))),
    ];
    inp.fire('change');
    await wait(60);

    assert((get('#treemap').innerHTML.match(/class="tile[\s"]/g) || []).length === 1, '只有附加库也能渲染');
    assert(get('#tbody').innerHTML.includes('Red Dead Redemption 2'), '显示附加库里的游戏');
  }

  /* 5) 封面：从 appcache/librarycache/<appid>/ 里取，转成 blob URL */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_730.acf', 'Steam/steamapps/appmanifest_730.acf',
        acf(730, 'Counter-Strike 2', Math.round(68.8 * GB))),
      // 同一目录有中英文两张，要挑没后缀的那张
      fakeFile('header_schinese.jpg', 'Steam/appcache/librarycache/730/header_schinese.jpg', 'cn'),
      fakeFile('header.jpg', 'Steam/appcache/librarycache/730/header.jpg', 'en'),
    ];
    inp.fire('change');
    await wait(90);

    const tm = get('#treemap').innerHTML;
    assert(tm.includes('has-cover'), '卡片用上了封面');
    assert(tm.includes('blob:fake/header.jpg'),
      '在 header.jpg 和 header_schinese.jpg 之间选了前者');
  }

  /* 6) 不在 librarycache 下的同名图片不该被当成封面 */
  {
    const get = makeEnv();
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_730.acf', 'Steam/steamapps/appmanifest_730.acf',
        acf(730, 'Counter-Strike 2', Math.round(68.8 * GB))),
      fakeFile('header.jpg', 'Steam/steamapps/common/SomeGame/header.jpg', 'x'),
    ];
    inp.fire('change');
    await wait(90);

    const tm6 = get('#treemap').innerHTML;
    assert(!tm6.includes('blob:fake'), '游戏目录里的 header.jpg 没被当成本地封面');
    assert(tm6.includes('cdn.cloudflare.steamstatic.com'), '本地没封面时用官方 CDN 兜底');
  }

  /* 7) 关掉 CDN 且本地没有封面时，退回纯色，不影响主流程 */
  {
    const get = makeEnv();
    get.sandbox.__STEAM_CDN_COVER = false;
    const inp = get('#dirInput');
    inp.files = [
      fakeFile('appmanifest_730.acf', 'Steam/steamapps/appmanifest_730.acf',
        acf(730, 'Counter-Strike 2', Math.round(68.8 * GB))),
    ];
    inp.fire('change');
    await wait(60);

    const tm = get('#treemap').innerHTML;
    assert(tm.includes('class="tile"') && !tm.includes('has-cover'), '没有封面时正常出纯色块');
  }

  console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
  process.exit(failed ? 1 : 0);
})();
