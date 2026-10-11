/* Swagger 文件数组扩展：一次选择多个原生 File，不上传、不读取文件正文。 */
"use strict";

/** 创建多选组件插件，使用 Swagger 自带 React/Immutable，不引入构建依赖。 */
function DkfFilePickerPlugin(system) {
  const React = system.React;
  const h = React.createElement;

  /** 从 Swagger 的受控值取出真实 File，忽略自动生成的字符串示例。 */
  function selectedFiles(value) {
    const values = value && typeof value.toArray === "function" ? value.toArray() : value;
    return Array.isArray(values) ? values.filter(isFile) : [];
  }

  /** 只认可浏览器原生文件对象，保持其身份以供 multipart 序列化。 */
  function isFile(value) {
    return value instanceof File;
  }

  /** 按文件名和实际浏览器大小初检；内容标识、Hash 和重复仍由服务端裁决。 */
  function fileWarnings(file, extensions, maxBytes) {
    const warnings = [];
    const name = file.name;
    const extension = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    if (!name.trim() || Array.from(name).length > 255 || /[\\/\x00-\x1f\x7f]/.test(name)) {
      warnings.push("文件名无效");
    }
    if (!extensions.includes(extension)) warnings.push("不支持此文件类型");
    if (file.size === 0) warnings.push("文件为空");
    if (file.size > maxBytes) warnings.push("超过单文件大小限制");
    return warnings;
  }

  /** 以易读单位展示大小，不将选择成功冒充内容验证成功。 */
  function displaySize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  }

  class FilePicker extends React.Component {
    /** 绑定原生输入引用和回调，文件列表由 Swagger 管理而非另建上传状态。 */
    constructor(props) {
      super(props);
      this.input = null;
      this.onSelect = this.onSelect.bind(this);
      this.onClear = this.onClear.bind(this);
      this.setInput = this.setInput.bind(this);
    }

    /** 保存原生输入引用，允许清空后再次选择相同文件。 */
    setInput(input) {
      this.input = input;
    }

    /** 选择完成后一次传入全部 File；取消选择不删除之前的有效选择。 */
    onSelect(event) {
      const files = Array.from(event.target.files || []);
      if (files.length) this.props.onChange(system.Im.List(files));
    }

    /** 清空整个选择列表，不影响后台文件或文档记录。 */
    onClear() {
      if (this.input) this.input.value = "";
      this.props.onChange(system.Im.List());
    }

    /** Swagger Reset 清空受控值时，同步清空原生输入的显示和本机选择。 */
    componentDidUpdate(previous) {
      if (selectedFiles(previous.value).length && !selectedFiles(this.props.value).length) {
        if (this.input) this.input.value = "";
      }
    }

    /** 渲染一个 multiple 文件控件与选择后初检；不静默删除不合格文件。 */
    render() {
      const files = selectedFiles(this.props.value);
      const options = this.props.schema.get("x-dkf-file-picker");
      const extensions = options.get("extensions").toArray();
      const maxFiles = options.get("maxFiles");
      const maxBytes = system.getConfigs().dkfUploadMaxBytes;
      const disabled = Boolean(this.props.disabled);

      /** 渲染单份文件的安全文字摘要，React 自动转义名称，不输出文件正文。 */
      function renderFile(file, index) {
        const warnings = fileWarnings(file, extensions, maxBytes);
        return h("li", { key: index, style: { overflowWrap: "anywhere" } },
          `${index + 1}. ${file.name}（${displaySize(file.size)}） — `,
          warnings.length ? `初检未通过：${warnings.join("；")}` : "初检通过（内容待服务端校验）");
      }

      return h("div", { className: "dkf-file-picker" },
        h("input", {
          type: "file", multiple: true, disabled: disabled, ref: this.setInput,
          onChange: this.onSelect, "aria-label": "选择文件（可单选或多选）",
          style: { width: "100%", maxWidth: "650px" },
        }),
        h("p", null, `打开文件窗口后可单选，或用 Ctrl / Shift 多选；每次最多 ${maxFiles} 份，单份最多 ${displaySize(maxBytes)}。`),
        h("div", { role: "status", "aria-live": "polite" },
          files.length ? `已选择 ${files.length} 份文件` : "尚未选择文件",
          files.length > maxFiles ? `；数量超限，提交将被拒绝，请重新选择不超过 ${maxFiles} 份` : ""),
        files.length ? h("ul", { style: { listStyle: "none", paddingLeft: 0 } }, files.map(renderFile)) : null,
        h("p", null, "初检仅检查名称、后缀、大小和数量，不读取内容；不会自动上传。点击 Execute 后，服务端逐文件最终校验并返回结果，不合格项不会被静默丢弃。"),
        h("button", { type: "button", className: "btn", disabled: disabled || !files.length, onClick: this.onClear }, "清空选择"));
    }
  }

  /** 仅包装显式标记的二进制数组，其他普通数组仍使用 Swagger 原组件。 */
  function wrapFileArray(Original) {
    /** 根据当前属性选择上传组件，不改变普通数组的增删和校验行为。 */
    function WrappedArray(props) {
      const schema = props.schema;
      const marked = schema && schema.get("x-dkf-file-picker");
      const binary = schema && schema.getIn(["items", "format"]) === "binary";
      return h(marked && binary ? FilePicker : Original, props);
    }
    return WrappedArray;
  }

  return { wrapComponents: { JsonSchema_array: wrapFileArray } };
}
