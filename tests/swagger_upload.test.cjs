/* 无浏览器依赖的插件行为测试；实际 Swagger 集成还需真实页面验收。 */
"use strict";

const assert = require("node:assert/strict");
const { File } = require("node:buffer");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../app/static/swagger_upload.js"), "utf8");

/** 模拟 Swagger Immutable 容器的最小接口，不改变 File 对象身份。 */
function immutable(value) {
  if (Array.isArray(value)) return { toArray: arrayValues };
  return { get: getValue, getIn: getNested };
  /** 返回数组的浅拷贝，使文件身份及顺序与真实 Immutable.List 相同。 */
  function arrayValues() { return value.slice(); }
  /** 读取属性，嵌套对象按相同方式包装。 */
  function getValue(key) {
    const result = value[key];
    return result && typeof result === "object" ? immutable(result) : result;
  }
  /** 按路径读取二进制格式，不对 File 对象做 JSON 转换。 */
  function getNested(keys) {
    let result = value;
    for (const key of keys) result = result && result[key];
    return result;
  }
}

/** 构建最小 React 外壳，仅观察节点与事件，不模拟网络或 DOM 上传。 */
function harness(options = {}) {
  const changes = [];
  class Component {
    /** 保留受控属性，让测试显式模拟 Swagger 的下一次渲染。 */
    constructor(props) { this.props = props; }
  }
  /** 记录 React 元素；文件名只能作为文字节点，不允许 HTML 插入。 */
  function createElement(type, props, ...children) { return { type, props, children: children.flat() }; }
  /** 模拟 Immutable.List，以浅包装保留所有 File。 */
  function List(files = []) { return immutable(Array.from(files)); }
  /** 提供页面公开的单文件限制。 */
  function getConfigs() { return { dkfUploadMaxBytes: 1024 }; }
  /** 观察所有传给 Swagger 的受控列表变化。 */
  function onChange(value) { changes.push(value.toArray()); }
  /** 普通数组必须回退的组件哨兵。 */
  function Original() {}
  const scope = { File };
  vm.createContext(scope);
  vm.runInContext(source, scope);
  const system = { React: { Component, createElement }, Im: { List }, getConfigs };
  const props = {
    schema: immutable({
      type: "array", items: { type: "string", format: "binary" },
      "x-dkf-file-picker": { extensions: ["txt", "md"], maxFiles: 50 },
      ...options.schema,
    }),
    value: List(options.files || []), onChange, disabled: options.disabled || false,
  };
  const plugin = scope.DkfFilePickerPlugin(system);
  const Wrapped = plugin.wrapComponents.JsonSchema_array(Original);
  const element = Wrapped(props);
  const picker = element.type === Original ? null : new element.type(element.props);
  return { props, picker, changes, element, Original, List };
}

/** 递归收集 React 节点，验证单控件与文本提示。 */
function nodes(tree) {
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...tree.children.flatMap(nodes)];
}

/** 收集纯文本子节点，检查选择后提示而不依赖 HTML 解析。 */
function text(tree) {
  if (typeof tree === "string") return tree;
  return tree && typeof tree === "object" ? tree.children.map(text).join(" ") : "";
}

/** 验证全部文件保持原生身份、顺序和 repeated file 字段形状。 */
function selectionTest() {
  const { picker, changes } = harness();
  const files = [new File(["first"], "甲.txt"), new File(["second"], "乙.md")];
  const inputs = nodes(picker.render()).filter(isInput);
  /** 精确定位原生文件输入，不把其他控件当作文件选择器。 */
  function isInput(node) { return node.type === "input" && node.props.type === "file"; }
  assert.equal(inputs.length, 1);
  assert.equal(inputs[0].props.multiple, true);
  picker.onSelect({ target: { files } });
  assert.equal(changes[0].length, 2);
  assert.equal(changes[0][0], files[0]);
  assert.equal(changes[0][1], files[1]);
  const form = new FormData();
  for (const file of changes[0]) form.append("file", file);
  assert.deepEqual(form.getAll("file").map(fileName), ["甲.txt", "乙.md"]);
  /** 检查 FormData 原生文件名未被替换为字符串或同一文件。 */
  function fileName(file) { return file.name; }
}
test("一个 multiple 控件保留多个原生 File 与顺序", selectionTest);

/** 验证只选一份、重新选择替换列表、取消及清空语义。 */
function replacementTest() {
  const { picker, changes, List } = harness();
  const file = new File(["one"], "单份.txt");
  picker.onSelect({ target: { files: [file] } });
  picker.onSelect({ target: { files: [] } });
  assert.equal(changes.length, 1);
  const newer = new File(["two"], "新.md");
  picker.onSelect({ target: { files: [newer] } });
  assert.equal(changes[1].length, 1);
  assert.equal(changes[1][0], newer);
  const input = { value: "C:\\fakepath\\新.md" };
  picker.setInput(input);
  picker.props.value = List([newer]);
  const previous = { ...picker.props };
  picker.props = { ...picker.props, value: List() };
  picker.componentDidUpdate(previous);
  assert.equal(input.value, "");
  picker.onClear();
  assert.equal(changes[2].length, 0);
}
test("单选、替换、取消和 Reset 清空不追加旧文件", replacementTest);

/** 检查格式、空文件、大小、名称初检，但所有不合格 File 仍保留。 */
function warningsTest() {
  const files = [
    new File(["safe"], "<img onerror=alert(1)>.TXT"),
    new File([], "空.txt"), new File(["bad"], "不支持.exe"),
    new File(["x".repeat(1025)], "超限.md"), new File(["x"], "../路径.txt"),
    new File(["x".repeat(1024)], "边界.txt"),
  ];
  const { picker, changes, List } = harness();
  picker.onSelect({ target: { files } });
  picker.props.value = List(changes[0]);
  const tree = picker.render();
  const rendered = text(tree);
  assert.match(rendered, /已选择 6 份/);
  assert.match(rendered, /文件为空/);
  assert.match(rendered, /不支持此文件类型/);
  assert.match(rendered, /超过单文件大小限制/);
  assert.match(rendered, /文件名无效/);
  assert.match(rendered, /边界.txt.*初检通过/);
  assert.equal(changes[0].length, 6);
  for (const node of nodes(tree)) assert.ok(!node.props?.dangerouslySetInnerHTML);
  assert.match(rendered, /<img onerror=alert\(1\)>.TXT/);
}
test("选择后初检不读取内容、不静默丢弃或插入文件名 HTML", warningsTest);

/** 数量超限提示不伪装成功、不截断为 50 个。 */
function countTest() {
  const files = [];
  for (let index = 0; index < 51; index++) files.push(new File(["x"], `${index}.txt`));
  const { picker, changes, List } = harness();
  picker.onSelect({ target: { files } });
  picker.props.value = List(changes[0]);
  assert.match(text(picker.render()), /数量超限/);
  assert.equal(changes[0].length, 51);
}
test("超过 50 份明确提示但不截断选择", countTest);

/** 普通数组或没有插件标记的文件数组保持 Swagger 默认行为。 */
function fallbackTest() {
  for (const schema of [
    { "x-dkf-file-picker": undefined },
    { items: { type: "string" } },
  ]) {
    const { element, Original } = harness({ schema });
    assert.equal(element.type, Original);
  }
}
test("非标记数组回退原控件", fallbackTest);

/** 忽略示例字符串，尊重只读状态，不呈现假文件或自动发请求。 */
function exampleTest() {
  const { picker, changes } = harness({ files: ["string", "string"], disabled: true });
  assert.match(text(picker.render()), /尚未选择文件/);
  assert.equal(changes.length, 0);
  for (const node of nodes(picker.render())) {
    if (node.type === "input" || node.type === "button") assert.equal(node.props.disabled, true);
  }
}
test("Swagger 示例不是选择结果，组件不自动上传", exampleTest);
