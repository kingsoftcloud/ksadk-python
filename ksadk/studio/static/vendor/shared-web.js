//#region node_modules/marked/lib/marked.esm.js
function e() {
	return {
		async: !1,
		breaks: !1,
		extensions: null,
		gfm: !0,
		hooks: null,
		pedantic: !1,
		renderer: null,
		silent: !1,
		tokenizer: null,
		walkTokens: null
	};
}
var t = e();
function n(e) {
	t = e;
}
var r = /[&<>"']/, i = new RegExp(r.source, "g"), a = /[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/, o = new RegExp(a.source, "g"), s = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	"\"": "&quot;",
	"'": "&#39;"
}, c = (e) => s[e];
function l(e, t) {
	if (t) {
		if (r.test(e)) return e.replace(i, c);
	} else if (a.test(e)) return e.replace(o, c);
	return e;
}
var u = /&(#(?:\d+)|(?:#x[0-9A-Fa-f]+)|(?:\w+));?/gi;
function d(e) {
	return e.replace(u, (e, t) => (t = t.toLowerCase(), t === "colon" ? ":" : t.charAt(0) === "#" ? t.charAt(1) === "x" ? String.fromCharCode(parseInt(t.substring(2), 16)) : String.fromCharCode(+t.substring(1)) : ""));
}
var f = /(^|[^\[])\^/g;
function p(e, t) {
	let n = typeof e == "string" ? e : e.source;
	t ||= "";
	let r = {
		replace: (e, t) => {
			let i = typeof t == "string" ? t : t.source;
			return i = i.replace(f, "$1"), n = n.replace(e, i), r;
		},
		getRegex: () => new RegExp(n, t)
	};
	return r;
}
function m(e) {
	try {
		e = encodeURI(e).replace(/%25/g, "%");
	} catch {
		return null;
	}
	return e;
}
var h = { exec: () => null };
function g(e, t) {
	let n = e.replace(/\|/g, (e, t, n) => {
		let r = !1, i = t;
		for (; --i >= 0 && n[i] === "\\";) r = !r;
		return r ? "|" : " |";
	}).split(/ \|/), r = 0;
	if (n[0].trim() || n.shift(), n.length > 0 && !n[n.length - 1].trim() && n.pop(), t) if (n.length > t) n.splice(t);
	else for (; n.length < t;) n.push("");
	for (; r < n.length; r++) n[r] = n[r].trim().replace(/\\\|/g, "|");
	return n;
}
function _(e, t, n) {
	let r = e.length;
	if (r === 0) return "";
	let i = 0;
	for (; i < r;) {
		let a = e.charAt(r - i - 1);
		if (a === t && !n) i++;
		else if (a !== t && n) i++;
		else break;
	}
	return e.slice(0, r - i);
}
function v(e, t) {
	if (e.indexOf(t[1]) === -1) return -1;
	let n = 0;
	for (let r = 0; r < e.length; r++) if (e[r] === "\\") r++;
	else if (e[r] === t[0]) n++;
	else if (e[r] === t[1] && (n--, n < 0)) return r;
	return -1;
}
function y(e, t, n, r) {
	let i = t.href, a = t.title ? l(t.title) : null, o = e[1].replace(/\\([\[\]])/g, "$1");
	if (e[0].charAt(0) !== "!") {
		r.state.inLink = !0;
		let e = {
			type: "link",
			raw: n,
			href: i,
			title: a,
			text: o,
			tokens: r.inlineTokens(o)
		};
		return r.state.inLink = !1, e;
	}
	return {
		type: "image",
		raw: n,
		href: i,
		title: a,
		text: l(o)
	};
}
function b(e, t) {
	let n = e.match(/^(\s+)(?:```)/);
	if (n === null) return t;
	let r = n[1];
	return t.split("\n").map((e) => {
		let t = e.match(/^\s+/);
		if (t === null) return e;
		let [n] = t;
		return n.length >= r.length ? e.slice(r.length) : e;
	}).join("\n");
}
var x = class {
	options;
	rules;
	lexer;
	constructor(e) {
		this.options = e || t;
	}
	space(e) {
		let t = this.rules.block.newline.exec(e);
		if (t && t[0].length > 0) return {
			type: "space",
			raw: t[0]
		};
	}
	code(e) {
		let t = this.rules.block.code.exec(e);
		if (t) {
			let e = t[0].replace(/^ {1,4}/gm, "");
			return {
				type: "code",
				raw: t[0],
				codeBlockStyle: "indented",
				text: this.options.pedantic ? e : _(e, "\n")
			};
		}
	}
	fences(e) {
		let t = this.rules.block.fences.exec(e);
		if (t) {
			let e = t[0], n = b(e, t[3] || "");
			return {
				type: "code",
				raw: e,
				lang: t[2] ? t[2].trim().replace(this.rules.inline.anyPunctuation, "$1") : t[2],
				text: n
			};
		}
	}
	heading(e) {
		let t = this.rules.block.heading.exec(e);
		if (t) {
			let e = t[2].trim();
			if (/#$/.test(e)) {
				let t = _(e, "#");
				(this.options.pedantic || !t || / $/.test(t)) && (e = t.trim());
			}
			return {
				type: "heading",
				raw: t[0],
				depth: t[1].length,
				text: e,
				tokens: this.lexer.inline(e)
			};
		}
	}
	hr(e) {
		let t = this.rules.block.hr.exec(e);
		if (t) return {
			type: "hr",
			raw: t[0]
		};
	}
	blockquote(e) {
		let t = this.rules.block.blockquote.exec(e);
		if (t) {
			let e = t[0].replace(/\n {0,3}((?:=+|-+) *)(?=\n|$)/g, "\n    $1");
			e = _(e.replace(/^ *>[ \t]?/gm, ""), "\n");
			let n = this.lexer.state.top;
			this.lexer.state.top = !0;
			let r = this.lexer.blockTokens(e);
			return this.lexer.state.top = n, {
				type: "blockquote",
				raw: t[0],
				tokens: r,
				text: e
			};
		}
	}
	list(e) {
		let t = this.rules.block.list.exec(e);
		if (t) {
			let n = t[1].trim(), r = n.length > 1, i = {
				type: "list",
				raw: "",
				ordered: r,
				start: r ? +n.slice(0, -1) : "",
				loose: !1,
				items: []
			};
			n = r ? `\\d{1,9}\\${n.slice(-1)}` : `\\${n}`, this.options.pedantic && (n = r ? n : "[*+-]");
			let a = RegExp(`^( {0,3}${n})((?:[\t ][^\\n]*)?(?:\\n|$))`), o = "", s = "", c = !1;
			for (; e;) {
				let n = !1;
				if (!(t = a.exec(e)) || this.rules.block.hr.test(e)) break;
				o = t[0], e = e.substring(o.length);
				let r = t[2].split("\n", 1)[0].replace(/^\t+/, (e) => " ".repeat(3 * e.length)), l = e.split("\n", 1)[0], u = 0;
				this.options.pedantic ? (u = 2, s = r.trimStart()) : (u = t[2].search(/[^ ]/), u = u > 4 ? 1 : u, s = r.slice(u), u += t[1].length);
				let d = !1;
				if (!r && /^ *$/.test(l) && (o += l + "\n", e = e.substring(l.length + 1), n = !0), !n) {
					let t = RegExp(`^ {0,${Math.min(3, u - 1)}}(?:[*+-]|\\d{1,9}[.)])((?:[ \t][^\\n]*)?(?:\\n|$))`), n = RegExp(`^ {0,${Math.min(3, u - 1)}}((?:- *){3,}|(?:_ *){3,}|(?:\\* *){3,})(?:\\n+|$)`), i = RegExp(`^ {0,${Math.min(3, u - 1)}}(?:\`\`\`|~~~)`), a = RegExp(`^ {0,${Math.min(3, u - 1)}}#`);
					for (; e;) {
						let c = e.split("\n", 1)[0];
						if (l = c, this.options.pedantic && (l = l.replace(/^ {1,4}(?=( {4})*[^ ])/g, "  ")), i.test(l) || a.test(l) || t.test(l) || n.test(e)) break;
						if (l.search(/[^ ]/) >= u || !l.trim()) s += "\n" + l.slice(u);
						else {
							if (d || r.search(/[^ ]/) >= 4 || i.test(r) || a.test(r) || n.test(r)) break;
							s += "\n" + l;
						}
						!d && !l.trim() && (d = !0), o += c + "\n", e = e.substring(c.length + 1), r = l.slice(u);
					}
				}
				i.loose || (c ? i.loose = !0 : /\n *\n *$/.test(o) && (c = !0));
				let f = null, p;
				this.options.gfm && (f = /^\[[ xX]\] /.exec(s), f && (p = f[0] !== "[ ] ", s = s.replace(/^\[[ xX]\] +/, ""))), i.items.push({
					type: "list_item",
					raw: o,
					task: !!f,
					checked: p,
					loose: !1,
					text: s,
					tokens: []
				}), i.raw += o;
			}
			i.items[i.items.length - 1].raw = o.trimEnd(), i.items[i.items.length - 1].text = s.trimEnd(), i.raw = i.raw.trimEnd();
			for (let e = 0; e < i.items.length; e++) if (this.lexer.state.top = !1, i.items[e].tokens = this.lexer.blockTokens(i.items[e].text, []), !i.loose) {
				let t = i.items[e].tokens.filter((e) => e.type === "space");
				i.loose = t.length > 0 && t.some((e) => /\n.*\n/.test(e.raw));
			}
			if (i.loose) for (let e = 0; e < i.items.length; e++) i.items[e].loose = !0;
			return i;
		}
	}
	html(e) {
		let t = this.rules.block.html.exec(e);
		if (t) return {
			type: "html",
			block: !0,
			raw: t[0],
			pre: t[1] === "pre" || t[1] === "script" || t[1] === "style",
			text: t[0]
		};
	}
	def(e) {
		let t = this.rules.block.def.exec(e);
		if (t) {
			let e = t[1].toLowerCase().replace(/\s+/g, " "), n = t[2] ? t[2].replace(/^<(.*)>$/, "$1").replace(this.rules.inline.anyPunctuation, "$1") : "", r = t[3] ? t[3].substring(1, t[3].length - 1).replace(this.rules.inline.anyPunctuation, "$1") : t[3];
			return {
				type: "def",
				tag: e,
				raw: t[0],
				href: n,
				title: r
			};
		}
	}
	table(e) {
		let t = this.rules.block.table.exec(e);
		if (!t || !/[:|]/.test(t[2])) return;
		let n = g(t[1]), r = t[2].replace(/^\||\| *$/g, "").split("|"), i = t[3] && t[3].trim() ? t[3].replace(/\n[ \t]*$/, "").split("\n") : [], a = {
			type: "table",
			raw: t[0],
			header: [],
			align: [],
			rows: []
		};
		if (n.length === r.length) {
			for (let e of r) /^ *-+: *$/.test(e) ? a.align.push("right") : /^ *:-+: *$/.test(e) ? a.align.push("center") : /^ *:-+ *$/.test(e) ? a.align.push("left") : a.align.push(null);
			for (let e of n) a.header.push({
				text: e,
				tokens: this.lexer.inline(e)
			});
			for (let e of i) a.rows.push(g(e, a.header.length).map((e) => ({
				text: e,
				tokens: this.lexer.inline(e)
			})));
			return a;
		}
	}
	lheading(e) {
		let t = this.rules.block.lheading.exec(e);
		if (t) return {
			type: "heading",
			raw: t[0],
			depth: t[2].charAt(0) === "=" ? 1 : 2,
			text: t[1],
			tokens: this.lexer.inline(t[1])
		};
	}
	paragraph(e) {
		let t = this.rules.block.paragraph.exec(e);
		if (t) {
			let e = t[1].charAt(t[1].length - 1) === "\n" ? t[1].slice(0, -1) : t[1];
			return {
				type: "paragraph",
				raw: t[0],
				text: e,
				tokens: this.lexer.inline(e)
			};
		}
	}
	text(e) {
		let t = this.rules.block.text.exec(e);
		if (t) return {
			type: "text",
			raw: t[0],
			text: t[0],
			tokens: this.lexer.inline(t[0])
		};
	}
	escape(e) {
		let t = this.rules.inline.escape.exec(e);
		if (t) return {
			type: "escape",
			raw: t[0],
			text: l(t[1])
		};
	}
	tag(e) {
		let t = this.rules.inline.tag.exec(e);
		if (t) return !this.lexer.state.inLink && /^<a /i.test(t[0]) ? this.lexer.state.inLink = !0 : this.lexer.state.inLink && /^<\/a>/i.test(t[0]) && (this.lexer.state.inLink = !1), !this.lexer.state.inRawBlock && /^<(pre|code|kbd|script)(\s|>)/i.test(t[0]) ? this.lexer.state.inRawBlock = !0 : this.lexer.state.inRawBlock && /^<\/(pre|code|kbd|script)(\s|>)/i.test(t[0]) && (this.lexer.state.inRawBlock = !1), {
			type: "html",
			raw: t[0],
			inLink: this.lexer.state.inLink,
			inRawBlock: this.lexer.state.inRawBlock,
			block: !1,
			text: t[0]
		};
	}
	link(e) {
		let t = this.rules.inline.link.exec(e);
		if (t) {
			let e = t[2].trim();
			if (!this.options.pedantic && /^</.test(e)) {
				if (!/>$/.test(e)) return;
				let t = _(e.slice(0, -1), "\\");
				if ((e.length - t.length) % 2 == 0) return;
			} else {
				let e = v(t[2], "()");
				if (e > -1) {
					let n = (t[0].indexOf("!") === 0 ? 5 : 4) + t[1].length + e;
					t[2] = t[2].substring(0, e), t[0] = t[0].substring(0, n).trim(), t[3] = "";
				}
			}
			let n = t[2], r = "";
			if (this.options.pedantic) {
				let e = /^([^'"]*[^\s])\s+(['"])(.*)\2/.exec(n);
				e && (n = e[1], r = e[3]);
			} else r = t[3] ? t[3].slice(1, -1) : "";
			return n = n.trim(), /^</.test(n) && (n = this.options.pedantic && !/>$/.test(e) ? n.slice(1) : n.slice(1, -1)), y(t, {
				href: n && n.replace(this.rules.inline.anyPunctuation, "$1"),
				title: r && r.replace(this.rules.inline.anyPunctuation, "$1")
			}, t[0], this.lexer);
		}
	}
	reflink(e, t) {
		let n;
		if ((n = this.rules.inline.reflink.exec(e)) || (n = this.rules.inline.nolink.exec(e))) {
			let e = t[(n[2] || n[1]).replace(/\s+/g, " ").toLowerCase()];
			if (!e) {
				let e = n[0].charAt(0);
				return {
					type: "text",
					raw: e,
					text: e
				};
			}
			return y(n, e, n[0], this.lexer);
		}
	}
	emStrong(e, t, n = "") {
		let r = this.rules.inline.emStrongLDelim.exec(e);
		if (r && !(r[3] && n.match(/[\p{L}\p{N}]/u)) && (!(r[1] || r[2]) || !n || this.rules.inline.punctuation.exec(n))) {
			let n = [...r[0]].length - 1, i, a, o = n, s = 0, c = r[0][0] === "*" ? this.rules.inline.emStrongRDelimAst : this.rules.inline.emStrongRDelimUnd;
			for (c.lastIndex = 0, t = t.slice(-1 * e.length + n); (r = c.exec(t)) != null;) {
				if (i = r[1] || r[2] || r[3] || r[4] || r[5] || r[6], !i) continue;
				if (a = [...i].length, r[3] || r[4]) {
					o += a;
					continue;
				}
				if ((r[5] || r[6]) && n % 3 && !((n + a) % 3)) {
					s += a;
					continue;
				}
				if (o -= a, o > 0) continue;
				a = Math.min(a, a + o + s);
				let t = [...r[0]][0].length, c = e.slice(0, n + r.index + t + a);
				if (Math.min(n, a) % 2) {
					let e = c.slice(1, -1);
					return {
						type: "em",
						raw: c,
						text: e,
						tokens: this.lexer.inlineTokens(e)
					};
				}
				let l = c.slice(2, -2);
				return {
					type: "strong",
					raw: c,
					text: l,
					tokens: this.lexer.inlineTokens(l)
				};
			}
		}
	}
	codespan(e) {
		let t = this.rules.inline.code.exec(e);
		if (t) {
			let e = t[2].replace(/\n/g, " "), n = /[^ ]/.test(e), r = /^ /.test(e) && / $/.test(e);
			return n && r && (e = e.substring(1, e.length - 1)), e = l(e, !0), {
				type: "codespan",
				raw: t[0],
				text: e
			};
		}
	}
	br(e) {
		let t = this.rules.inline.br.exec(e);
		if (t) return {
			type: "br",
			raw: t[0]
		};
	}
	del(e) {
		let t = this.rules.inline.del.exec(e);
		if (t) return {
			type: "del",
			raw: t[0],
			text: t[2],
			tokens: this.lexer.inlineTokens(t[2])
		};
	}
	autolink(e) {
		let t = this.rules.inline.autolink.exec(e);
		if (t) {
			let e, n;
			return t[2] === "@" ? (e = l(t[1]), n = "mailto:" + e) : (e = l(t[1]), n = e), {
				type: "link",
				raw: t[0],
				text: e,
				href: n,
				tokens: [{
					type: "text",
					raw: e,
					text: e
				}]
			};
		}
	}
	url(e) {
		let t;
		if (t = this.rules.inline.url.exec(e)) {
			let e, n;
			if (t[2] === "@") e = l(t[0]), n = "mailto:" + e;
			else {
				let r;
				do
					r = t[0], t[0] = this.rules.inline._backpedal.exec(t[0])?.[0] ?? "";
				while (r !== t[0]);
				e = l(t[0]), n = t[1] === "www." ? "http://" + t[0] : t[0];
			}
			return {
				type: "link",
				raw: t[0],
				text: e,
				href: n,
				tokens: [{
					type: "text",
					raw: e,
					text: e
				}]
			};
		}
	}
	inlineText(e) {
		let t = this.rules.inline.text.exec(e);
		if (t) {
			let e;
			return e = this.lexer.state.inRawBlock ? t[0] : l(t[0]), {
				type: "text",
				raw: t[0],
				text: e
			};
		}
	}
}, ee = /^(?: *(?:\n|$))+/, te = /^( {4}[^\n]+(?:\n(?: *(?:\n|$))*)?)+/, ne = /^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))(?: {0,3}\1[~`]* *(?=\n|$)|$)/, S = /^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)/, re = /^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)/, C = /(?:[*+-]|\d{1,9}[.)])/, w = p(/^(?!bull |blockCode|fences|blockquote|heading|html)((?:.|\n(?!\s*?\n|bull |blockCode|fences|blockquote|heading|html))+?)\n {0,3}(=+|-+) *(?:\n+|$)/).replace(/bull/g, C).replace(/blockCode/g, / {4}/).replace(/fences/g, / {0,3}(?:`{3,}|~{3,})/).replace(/blockquote/g, / {0,3}>/).replace(/heading/g, / {0,3}#{1,6}/).replace(/html/g, / {0,3}<[^\n>]+>\n/).getRegex(), T = /^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table| +\n)[^\n]+)*)/, ie = /^[^\n]+/, E = /(?!\s*\])(?:\\.|[^\[\]\\])+/, ae = p(/^ {0,3}\[(label)\]: *(?:\n *)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n *)?| *\n *)(title))? *(?:\n+|$)/).replace("label", E).replace("title", /(?:"(?:\\"?|[^"\\])*"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\))/).getRegex(), oe = p(/^( {0,3}bull)([ \t][^\n]+?)?(?:\n|$)/).replace(/bull/g, C).getRegex(), D = "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul", O = /<!--(?:-?>|[\s\S]*?(?:-->|$))/, se = p("^ {0,3}(?:<(script|pre|style|textarea)[\\s>][\\s\\S]*?(?:</\\1>[^\\n]*\\n+|$)|comment[^\\n]*(\\n+|$)|<\\?[\\s\\S]*?(?:\\?>\\n*|$)|<![A-Z][\\s\\S]*?(?:>\\n*|$)|<!\\[CDATA\\[[\\s\\S]*?(?:\\]\\]>\\n*|$)|</?(tag)(?: +|\\n|/?>)[\\s\\S]*?(?:(?:\\n *)+\\n|$)|<(?!script|pre|style|textarea)([a-z][\\w-]*)(?:attribute)*? */?>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$)|</(?!script|pre|style|textarea)[a-z][\\w-]*\\s*>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$))", "i").replace("comment", O).replace("tag", D).replace("attribute", / +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?/).getRegex(), k = p(T).replace("hr", S).replace("heading", " {0,3}#{1,6}(?:\\s|$)").replace("|lheading", "").replace("|table", "").replace("blockquote", " {0,3}>").replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list", " {0,3}(?:[*+-]|1[.)]) ").replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag", D).getRegex(), A = {
	blockquote: p(/^( {0,3}> ?(paragraph|[^\n]*)(?:\n|$))+/).replace("paragraph", k).getRegex(),
	code: te,
	def: ae,
	fences: ne,
	heading: re,
	hr: S,
	html: se,
	lheading: w,
	list: oe,
	newline: ee,
	paragraph: k,
	table: h,
	text: ie
}, j = p("^ *([^\\n ].*)\\n {0,3}((?:\\| *)?:?-+:? *(?:\\| *:?-+:? *)*(?:\\| *)?)(?:\\n((?:(?! *\\n|hr|heading|blockquote|code|fences|list|html).*(?:\\n|$))*)\\n*|$)").replace("hr", S).replace("heading", " {0,3}#{1,6}(?:\\s|$)").replace("blockquote", " {0,3}>").replace("code", " {4}[^\\n]").replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list", " {0,3}(?:[*+-]|1[.)]) ").replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag", D).getRegex(), ce = {
	...A,
	table: j,
	paragraph: p(T).replace("hr", S).replace("heading", " {0,3}#{1,6}(?:\\s|$)").replace("|lheading", "").replace("table", j).replace("blockquote", " {0,3}>").replace("fences", " {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list", " {0,3}(?:[*+-]|1[.)]) ").replace("html", "</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag", D).getRegex()
}, le = {
	...A,
	html: p("^ *(?:comment *(?:\\n|\\s*$)|<(tag)[\\s\\S]+?</\\1> *(?:\\n{2,}|\\s*$)|<tag(?:\"[^\"]*\"|'[^']*'|\\s[^'\"/>\\s]*)*?/?> *(?:\\n{2,}|\\s*$))").replace("comment", O).replace(/tag/g, "(?!(?:a|em|strong|small|s|cite|q|dfn|abbr|data|time|code|var|samp|kbd|sub|sup|i|b|u|mark|ruby|rt|rp|bdi|bdo|span|br|wbr|ins|del|img)\\b)\\w+(?!:|[^\\w\\s@]*@)\\b").getRegex(),
	def: /^ *\[([^\]]+)\]: *<?([^\s>]+)>?(?: +(["(][^\n]+[")]))? *(?:\n+|$)/,
	heading: /^(#{1,6})(.*)(?:\n+|$)/,
	fences: h,
	lheading: /^(.+?)\n {0,3}(=+|-+) *(?:\n+|$)/,
	paragraph: p(T).replace("hr", S).replace("heading", " *#{1,6} *[^\n]").replace("lheading", w).replace("|table", "").replace("blockquote", " {0,3}>").replace("|fences", "").replace("|list", "").replace("|html", "").replace("|tag", "").getRegex()
}, M = /^\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])/, ue = /^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/, N = /^( {2,}|\\)\n(?!\s*$)/, de = /^(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|$)|[^ ](?= {2,}\n)))/, P = "\\p{P}\\p{S}", fe = p(/^((?![*_])[\spunctuation])/, "u").replace(/punctuation/g, P).getRegex(), pe = /\[[^[\]]*?\]\([^\(\)]*?\)|`[^`]*?`|<[^<>]*?>/g, me = p(/^(?:\*+(?:((?!\*)[punct])|[^\s*]))|^_+(?:((?!_)[punct])|([^\s_]))/, "u").replace(/punct/g, P).getRegex(), he = p("^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)|[^*]+(?=[^*])|(?!\\*)[punct](\\*+)(?=[\\s]|$)|[^punct\\s](\\*+)(?!\\*)(?=[punct\\s]|$)|(?!\\*)[punct\\s](\\*+)(?=[^punct\\s])|[\\s](\\*+)(?!\\*)(?=[punct])|(?!\\*)[punct](\\*+)(?!\\*)(?=[punct])|[^punct\\s](\\*+)(?=[^punct\\s])", "gu").replace(/punct/g, P).getRegex(), ge = p("^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)|[^_]+(?=[^_])|(?!_)[punct](_+)(?=[\\s]|$)|[^punct\\s](_+)(?!_)(?=[punct\\s]|$)|(?!_)[punct\\s](_+)(?=[^punct\\s])|[\\s](_+)(?!_)(?=[punct])|(?!_)[punct](_+)(?!_)(?=[punct])", "gu").replace(/punct/g, P).getRegex(), _e = p(/\\([punct])/, "gu").replace(/punct/g, P).getRegex(), ve = p(/^<(scheme:[^\s\x00-\x1f<>]*|email)>/).replace("scheme", /[a-zA-Z][a-zA-Z0-9+.-]{1,31}/).replace("email", /[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_])/).getRegex(), ye = p(O).replace("(?:-->|$)", "-->").getRegex(), be = p("^comment|^</[a-zA-Z][\\w:-]*\\s*>|^<[a-zA-Z][\\w-]*(?:attribute)*?\\s*/?>|^<\\?[\\s\\S]*?\\?>|^<![a-zA-Z]+\\s[\\s\\S]*?>|^<!\\[CDATA\\[[\\s\\S]*?\\]\\]>").replace("comment", ye).replace("attribute", /\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*"[^"]*"|\s*=\s*'[^']*'|\s*=\s*[^\s"'=<>`]+)?/).getRegex(), F = /(?:\[(?:\\.|[^\[\]\\])*\]|\\.|`[^`]*`|[^\[\]\\`])*?/, xe = p(/^!?\[(label)\]\(\s*(href)(?:\s+(title))?\s*\)/).replace("label", F).replace("href", /<(?:\\.|[^\n<>\\])+>|[^\s\x00-\x1f]*/).replace("title", /"(?:\\"?|[^"\\])*"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)/).getRegex(), I = p(/^!?\[(label)\]\[(ref)\]/).replace("label", F).replace("ref", E).getRegex(), L = p(/^!?\[(ref)\](?:\[\])?/).replace("ref", E).getRegex(), R = {
	_backpedal: h,
	anyPunctuation: _e,
	autolink: ve,
	blockSkip: pe,
	br: N,
	code: ue,
	del: h,
	emStrongLDelim: me,
	emStrongRDelimAst: he,
	emStrongRDelimUnd: ge,
	escape: M,
	link: xe,
	nolink: L,
	punctuation: fe,
	reflink: I,
	reflinkSearch: p("reflink|nolink(?!\\()", "g").replace("reflink", I).replace("nolink", L).getRegex(),
	tag: be,
	text: de,
	url: h
}, Se = {
	...R,
	link: p(/^!?\[(label)\]\((.*?)\)/).replace("label", F).getRegex(),
	reflink: p(/^!?\[(label)\]\s*\[([^\]]*)\]/).replace("label", F).getRegex()
}, z = {
	...R,
	escape: p(M).replace("])", "~|])").getRegex(),
	url: p(/^((?:ftp|https?):\/\/|www\.)(?:[a-zA-Z0-9\-]+\.?)+[^\s<]*|^email/, "i").replace("email", /[A-Za-z0-9._+-]+(@)[a-zA-Z0-9-_]+(?:\.[a-zA-Z0-9-_]*[a-zA-Z0-9])+(?![-_])/).getRegex(),
	_backpedal: /(?:[^?!.,:;*_'"~()&]+|\([^)]*\)|&(?![a-zA-Z0-9]+;$)|[?!.,:;*_'"~)]+(?!$))+/,
	del: /^(~~?)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/,
	text: /^([`~]+|[^`~])(?:(?= {2,}\n)|(?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)|[\s\S]*?(?:(?=[\\<!\[`*~_]|\b_|https?:\/\/|ftp:\/\/|www\.|$)|[^ ](?= {2,}\n)|[^a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-](?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)))/
}, Ce = {
	...z,
	br: p(N).replace("{2,}", "*").getRegex(),
	text: p(z.text).replace("\\b_", "\\b_| {2,}\\n").replace(/\{2,\}/g, "*").getRegex()
}, B = {
	normal: A,
	gfm: ce,
	pedantic: le
}, V = {
	normal: R,
	gfm: z,
	breaks: Ce,
	pedantic: Se
}, H = class e {
	tokens;
	options;
	state;
	tokenizer;
	inlineQueue;
	constructor(e) {
		this.tokens = [], this.tokens.links = Object.create(null), this.options = e || t, this.options.tokenizer = this.options.tokenizer || new x(), this.tokenizer = this.options.tokenizer, this.tokenizer.options = this.options, this.tokenizer.lexer = this, this.inlineQueue = [], this.state = {
			inLink: !1,
			inRawBlock: !1,
			top: !0
		};
		let n = {
			block: B.normal,
			inline: V.normal
		};
		this.options.pedantic ? (n.block = B.pedantic, n.inline = V.pedantic) : this.options.gfm && (n.block = B.gfm, n.inline = this.options.breaks ? V.breaks : V.gfm), this.tokenizer.rules = n;
	}
	static get rules() {
		return {
			block: B,
			inline: V
		};
	}
	static lex(t, n) {
		return new e(n).lex(t);
	}
	static lexInline(t, n) {
		return new e(n).inlineTokens(t);
	}
	lex(e) {
		e = e.replace(/\r\n|\r/g, "\n"), this.blockTokens(e, this.tokens);
		for (let e = 0; e < this.inlineQueue.length; e++) {
			let t = this.inlineQueue[e];
			this.inlineTokens(t.src, t.tokens);
		}
		return this.inlineQueue = [], this.tokens;
	}
	blockTokens(e, t = []) {
		e = this.options.pedantic ? e.replace(/\t/g, "    ").replace(/^ +$/gm, "") : e.replace(/^( *)(\t+)/gm, (e, t, n) => t + "    ".repeat(n.length));
		let n, r, i, a;
		for (; e;) if (!(this.options.extensions && this.options.extensions.block && this.options.extensions.block.some((r) => (n = r.call({ lexer: this }, e, t)) ? (e = e.substring(n.raw.length), t.push(n), !0) : !1))) {
			if (n = this.tokenizer.space(e)) {
				e = e.substring(n.raw.length), n.raw.length === 1 && t.length > 0 ? t[t.length - 1].raw += "\n" : t.push(n);
				continue;
			}
			if (n = this.tokenizer.code(e)) {
				e = e.substring(n.raw.length), r = t[t.length - 1], r && (r.type === "paragraph" || r.type === "text") ? (r.raw += "\n" + n.raw, r.text += "\n" + n.text, this.inlineQueue[this.inlineQueue.length - 1].src = r.text) : t.push(n);
				continue;
			}
			if (n = this.tokenizer.fences(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.heading(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.hr(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.blockquote(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.list(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.html(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.def(e)) {
				e = e.substring(n.raw.length), r = t[t.length - 1], r && (r.type === "paragraph" || r.type === "text") ? (r.raw += "\n" + n.raw, r.text += "\n" + n.raw, this.inlineQueue[this.inlineQueue.length - 1].src = r.text) : this.tokens.links[n.tag] || (this.tokens.links[n.tag] = {
					href: n.href,
					title: n.title
				});
				continue;
			}
			if (n = this.tokenizer.table(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.lheading(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (i = e, this.options.extensions && this.options.extensions.startBlock) {
				let t = Infinity, n = e.slice(1), r;
				this.options.extensions.startBlock.forEach((e) => {
					r = e.call({ lexer: this }, n), typeof r == "number" && r >= 0 && (t = Math.min(t, r));
				}), t < Infinity && t >= 0 && (i = e.substring(0, t + 1));
			}
			if (this.state.top && (n = this.tokenizer.paragraph(i))) {
				r = t[t.length - 1], a && r.type === "paragraph" ? (r.raw += "\n" + n.raw, r.text += "\n" + n.text, this.inlineQueue.pop(), this.inlineQueue[this.inlineQueue.length - 1].src = r.text) : t.push(n), a = i.length !== e.length, e = e.substring(n.raw.length);
				continue;
			}
			if (n = this.tokenizer.text(e)) {
				e = e.substring(n.raw.length), r = t[t.length - 1], r && r.type === "text" ? (r.raw += "\n" + n.raw, r.text += "\n" + n.text, this.inlineQueue.pop(), this.inlineQueue[this.inlineQueue.length - 1].src = r.text) : t.push(n);
				continue;
			}
			if (e) {
				let t = "Infinite loop on byte: " + e.charCodeAt(0);
				if (this.options.silent) {
					console.error(t);
					break;
				}
				throw Error(t);
			}
		}
		return this.state.top = !0, t;
	}
	inline(e, t = []) {
		return this.inlineQueue.push({
			src: e,
			tokens: t
		}), t;
	}
	inlineTokens(e, t = []) {
		let n, r, i, a = e, o, s, c;
		if (this.tokens.links) {
			let e = Object.keys(this.tokens.links);
			if (e.length > 0) for (; (o = this.tokenizer.rules.inline.reflinkSearch.exec(a)) != null;) e.includes(o[0].slice(o[0].lastIndexOf("[") + 1, -1)) && (a = a.slice(0, o.index) + "[" + "a".repeat(o[0].length - 2) + "]" + a.slice(this.tokenizer.rules.inline.reflinkSearch.lastIndex));
		}
		for (; (o = this.tokenizer.rules.inline.blockSkip.exec(a)) != null;) a = a.slice(0, o.index) + "[" + "a".repeat(o[0].length - 2) + "]" + a.slice(this.tokenizer.rules.inline.blockSkip.lastIndex);
		for (; (o = this.tokenizer.rules.inline.anyPunctuation.exec(a)) != null;) a = a.slice(0, o.index) + "++" + a.slice(this.tokenizer.rules.inline.anyPunctuation.lastIndex);
		for (; e;) if (s || (c = ""), s = !1, !(this.options.extensions && this.options.extensions.inline && this.options.extensions.inline.some((r) => (n = r.call({ lexer: this }, e, t)) ? (e = e.substring(n.raw.length), t.push(n), !0) : !1))) {
			if (n = this.tokenizer.escape(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.tag(e)) {
				e = e.substring(n.raw.length), r = t[t.length - 1], r && n.type === "text" && r.type === "text" ? (r.raw += n.raw, r.text += n.text) : t.push(n);
				continue;
			}
			if (n = this.tokenizer.link(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.reflink(e, this.tokens.links)) {
				e = e.substring(n.raw.length), r = t[t.length - 1], r && n.type === "text" && r.type === "text" ? (r.raw += n.raw, r.text += n.text) : t.push(n);
				continue;
			}
			if (n = this.tokenizer.emStrong(e, a, c)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.codespan(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.br(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.del(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (n = this.tokenizer.autolink(e)) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (!this.state.inLink && (n = this.tokenizer.url(e))) {
				e = e.substring(n.raw.length), t.push(n);
				continue;
			}
			if (i = e, this.options.extensions && this.options.extensions.startInline) {
				let t = Infinity, n = e.slice(1), r;
				this.options.extensions.startInline.forEach((e) => {
					r = e.call({ lexer: this }, n), typeof r == "number" && r >= 0 && (t = Math.min(t, r));
				}), t < Infinity && t >= 0 && (i = e.substring(0, t + 1));
			}
			if (n = this.tokenizer.inlineText(i)) {
				e = e.substring(n.raw.length), n.raw.slice(-1) !== "_" && (c = n.raw.slice(-1)), s = !0, r = t[t.length - 1], r && r.type === "text" ? (r.raw += n.raw, r.text += n.text) : t.push(n);
				continue;
			}
			if (e) {
				let t = "Infinite loop on byte: " + e.charCodeAt(0);
				if (this.options.silent) {
					console.error(t);
					break;
				}
				throw Error(t);
			}
		}
		return t;
	}
}, U = class {
	options;
	constructor(e) {
		this.options = e || t;
	}
	code(e, t, n) {
		let r = (t || "").match(/^\S*/)?.[0];
		return e = e.replace(/\n$/, "") + "\n", r ? "<pre><code class=\"language-" + l(r) + "\">" + (n ? e : l(e, !0)) + "</code></pre>\n" : "<pre><code>" + (n ? e : l(e, !0)) + "</code></pre>\n";
	}
	blockquote(e) {
		return `<blockquote>\n${e}</blockquote>\n`;
	}
	html(e, t) {
		return e;
	}
	heading(e, t, n) {
		return `<h${t}>${e}</h${t}>\n`;
	}
	hr() {
		return "<hr>\n";
	}
	list(e, t, n) {
		let r = t ? "ol" : "ul", i = t && n !== 1 ? " start=\"" + n + "\"" : "";
		return "<" + r + i + ">\n" + e + "</" + r + ">\n";
	}
	listitem(e, t, n) {
		return `<li>${e}</li>\n`;
	}
	checkbox(e) {
		return "<input " + (e ? "checked=\"\" " : "") + "disabled=\"\" type=\"checkbox\">";
	}
	paragraph(e) {
		return `<p>${e}</p>\n`;
	}
	table(e, t) {
		return t &&= `<tbody>${t}</tbody>`, "<table>\n<thead>\n" + e + "</thead>\n" + t + "</table>\n";
	}
	tablerow(e) {
		return `<tr>\n${e}</tr>\n`;
	}
	tablecell(e, t) {
		let n = t.header ? "th" : "td";
		return (t.align ? `<${n} align="${t.align}">` : `<${n}>`) + e + `</${n}>\n`;
	}
	strong(e) {
		return `<strong>${e}</strong>`;
	}
	em(e) {
		return `<em>${e}</em>`;
	}
	codespan(e) {
		return `<code>${e}</code>`;
	}
	br() {
		return "<br>";
	}
	del(e) {
		return `<del>${e}</del>`;
	}
	link(e, t, n) {
		let r = m(e);
		if (r === null) return n;
		e = r;
		let i = "<a href=\"" + e + "\"";
		return t && (i += " title=\"" + t + "\""), i += ">" + n + "</a>", i;
	}
	image(e, t, n) {
		let r = m(e);
		if (r === null) return n;
		e = r;
		let i = `<img src="${e}" alt="${n}"`;
		return t && (i += ` title="${t}"`), i += ">", i;
	}
	text(e) {
		return e;
	}
}, W = class {
	strong(e) {
		return e;
	}
	em(e) {
		return e;
	}
	codespan(e) {
		return e;
	}
	del(e) {
		return e;
	}
	html(e) {
		return e;
	}
	text(e) {
		return e;
	}
	link(e, t, n) {
		return "" + n;
	}
	image(e, t, n) {
		return "" + n;
	}
	br() {
		return "";
	}
}, G = class e {
	options;
	renderer;
	textRenderer;
	constructor(e) {
		this.options = e || t, this.options.renderer = this.options.renderer || new U(), this.renderer = this.options.renderer, this.renderer.options = this.options, this.textRenderer = new W();
	}
	static parse(t, n) {
		return new e(n).parse(t);
	}
	static parseInline(t, n) {
		return new e(n).parseInline(t);
	}
	parse(e, t = !0) {
		let n = "";
		for (let r = 0; r < e.length; r++) {
			let i = e[r];
			if (this.options.extensions && this.options.extensions.renderers && this.options.extensions.renderers[i.type]) {
				let e = i, t = this.options.extensions.renderers[e.type].call({ parser: this }, e);
				if (t !== !1 || ![
					"space",
					"hr",
					"heading",
					"code",
					"table",
					"blockquote",
					"list",
					"html",
					"paragraph",
					"text"
				].includes(e.type)) {
					n += t || "";
					continue;
				}
			}
			switch (i.type) {
				case "space": continue;
				case "hr":
					n += this.renderer.hr();
					continue;
				case "heading": {
					let e = i;
					n += this.renderer.heading(this.parseInline(e.tokens), e.depth, d(this.parseInline(e.tokens, this.textRenderer)));
					continue;
				}
				case "code": {
					let e = i;
					n += this.renderer.code(e.text, e.lang, !!e.escaped);
					continue;
				}
				case "table": {
					let e = i, t = "", r = "";
					for (let t = 0; t < e.header.length; t++) r += this.renderer.tablecell(this.parseInline(e.header[t].tokens), {
						header: !0,
						align: e.align[t]
					});
					t += this.renderer.tablerow(r);
					let a = "";
					for (let t = 0; t < e.rows.length; t++) {
						let n = e.rows[t];
						r = "";
						for (let t = 0; t < n.length; t++) r += this.renderer.tablecell(this.parseInline(n[t].tokens), {
							header: !1,
							align: e.align[t]
						});
						a += this.renderer.tablerow(r);
					}
					n += this.renderer.table(t, a);
					continue;
				}
				case "blockquote": {
					let e = i, t = this.parse(e.tokens);
					n += this.renderer.blockquote(t);
					continue;
				}
				case "list": {
					let e = i, t = e.ordered, r = e.start, a = e.loose, o = "";
					for (let t = 0; t < e.items.length; t++) {
						let n = e.items[t], r = n.checked, i = n.task, s = "";
						if (n.task) {
							let e = this.renderer.checkbox(!!r);
							a ? n.tokens.length > 0 && n.tokens[0].type === "paragraph" ? (n.tokens[0].text = e + " " + n.tokens[0].text, n.tokens[0].tokens && n.tokens[0].tokens.length > 0 && n.tokens[0].tokens[0].type === "text" && (n.tokens[0].tokens[0].text = e + " " + n.tokens[0].tokens[0].text)) : n.tokens.unshift({
								type: "text",
								text: e + " "
							}) : s += e + " ";
						}
						s += this.parse(n.tokens, a), o += this.renderer.listitem(s, i, !!r);
					}
					n += this.renderer.list(o, t, r);
					continue;
				}
				case "html": {
					let e = i;
					n += this.renderer.html(e.text, e.block);
					continue;
				}
				case "paragraph": {
					let e = i;
					n += this.renderer.paragraph(this.parseInline(e.tokens));
					continue;
				}
				case "text": {
					let a = i, o = a.tokens ? this.parseInline(a.tokens) : a.text;
					for (; r + 1 < e.length && e[r + 1].type === "text";) a = e[++r], o += "\n" + (a.tokens ? this.parseInline(a.tokens) : a.text);
					n += t ? this.renderer.paragraph(o) : o;
					continue;
				}
				default: {
					let e = "Token with \"" + i.type + "\" type was not found.";
					if (this.options.silent) return console.error(e), "";
					throw Error(e);
				}
			}
		}
		return n;
	}
	parseInline(e, t) {
		t ||= this.renderer;
		let n = "";
		for (let r = 0; r < e.length; r++) {
			let i = e[r];
			if (this.options.extensions && this.options.extensions.renderers && this.options.extensions.renderers[i.type]) {
				let e = this.options.extensions.renderers[i.type].call({ parser: this }, i);
				if (e !== !1 || ![
					"escape",
					"html",
					"link",
					"image",
					"strong",
					"em",
					"codespan",
					"br",
					"del",
					"text"
				].includes(i.type)) {
					n += e || "";
					continue;
				}
			}
			switch (i.type) {
				case "escape": {
					let e = i;
					n += t.text(e.text);
					break;
				}
				case "html": {
					let e = i;
					n += t.html(e.text);
					break;
				}
				case "link": {
					let e = i;
					n += t.link(e.href, e.title, this.parseInline(e.tokens, t));
					break;
				}
				case "image": {
					let e = i;
					n += t.image(e.href, e.title, e.text);
					break;
				}
				case "strong": {
					let e = i;
					n += t.strong(this.parseInline(e.tokens, t));
					break;
				}
				case "em": {
					let e = i;
					n += t.em(this.parseInline(e.tokens, t));
					break;
				}
				case "codespan": {
					let e = i;
					n += t.codespan(e.text);
					break;
				}
				case "br":
					n += t.br();
					break;
				case "del": {
					let e = i;
					n += t.del(this.parseInline(e.tokens, t));
					break;
				}
				case "text": {
					let e = i;
					n += t.text(e.text);
					break;
				}
				default: {
					let e = "Token with \"" + i.type + "\" type was not found.";
					if (this.options.silent) return console.error(e), "";
					throw Error(e);
				}
			}
		}
		return n;
	}
}, K = class {
	options;
	constructor(e) {
		this.options = e || t;
	}
	static passThroughHooks = /* @__PURE__ */ new Set([
		"preprocess",
		"postprocess",
		"processAllTokens"
	]);
	preprocess(e) {
		return e;
	}
	postprocess(e) {
		return e;
	}
	processAllTokens(e) {
		return e;
	}
}, q = new class {
	defaults = e();
	options = this.setOptions;
	parse = this.#e(H.lex, G.parse);
	parseInline = this.#e(H.lexInline, G.parseInline);
	Parser = G;
	Renderer = U;
	TextRenderer = W;
	Lexer = H;
	Tokenizer = x;
	Hooks = K;
	constructor(...e) {
		this.use(...e);
	}
	walkTokens(e, t) {
		let n = [];
		for (let r of e) switch (n = n.concat(t.call(this, r)), r.type) {
			case "table": {
				let e = r;
				for (let r of e.header) n = n.concat(this.walkTokens(r.tokens, t));
				for (let r of e.rows) for (let e of r) n = n.concat(this.walkTokens(e.tokens, t));
				break;
			}
			case "list": {
				let e = r;
				n = n.concat(this.walkTokens(e.items, t));
				break;
			}
			default: {
				let e = r;
				this.defaults.extensions?.childTokens?.[e.type] ? this.defaults.extensions.childTokens[e.type].forEach((r) => {
					let i = e[r].flat(Infinity);
					n = n.concat(this.walkTokens(i, t));
				}) : e.tokens && (n = n.concat(this.walkTokens(e.tokens, t)));
			}
		}
		return n;
	}
	use(...e) {
		let t = this.defaults.extensions || {
			renderers: {},
			childTokens: {}
		};
		return e.forEach((e) => {
			let n = { ...e };
			if (n.async = this.defaults.async || n.async || !1, e.extensions && (e.extensions.forEach((e) => {
				if (!e.name) throw Error("extension name required");
				if ("renderer" in e) {
					let n = t.renderers[e.name];
					n ? t.renderers[e.name] = function(...t) {
						let r = e.renderer.apply(this, t);
						return r === !1 && (r = n.apply(this, t)), r;
					} : t.renderers[e.name] = e.renderer;
				}
				if ("tokenizer" in e) {
					if (!e.level || e.level !== "block" && e.level !== "inline") throw Error("extension level must be 'block' or 'inline'");
					let n = t[e.level];
					n ? n.unshift(e.tokenizer) : t[e.level] = [e.tokenizer], e.start && (e.level === "block" ? t.startBlock ? t.startBlock.push(e.start) : t.startBlock = [e.start] : e.level === "inline" && (t.startInline ? t.startInline.push(e.start) : t.startInline = [e.start]));
				}
				"childTokens" in e && e.childTokens && (t.childTokens[e.name] = e.childTokens);
			}), n.extensions = t), e.renderer) {
				let t = this.defaults.renderer || new U(this.defaults);
				for (let n in e.renderer) {
					if (!(n in t)) throw Error(`renderer '${n}' does not exist`);
					if (n === "options") continue;
					let r = n, i = e.renderer[r], a = t[r];
					t[r] = (...e) => {
						let n = i.apply(t, e);
						return n === !1 && (n = a.apply(t, e)), n || "";
					};
				}
				n.renderer = t;
			}
			if (e.tokenizer) {
				let t = this.defaults.tokenizer || new x(this.defaults);
				for (let n in e.tokenizer) {
					if (!(n in t)) throw Error(`tokenizer '${n}' does not exist`);
					if ([
						"options",
						"rules",
						"lexer"
					].includes(n)) continue;
					let r = n, i = e.tokenizer[r], a = t[r];
					t[r] = (...e) => {
						let n = i.apply(t, e);
						return n === !1 && (n = a.apply(t, e)), n;
					};
				}
				n.tokenizer = t;
			}
			if (e.hooks) {
				let t = this.defaults.hooks || new K();
				for (let n in e.hooks) {
					if (!(n in t)) throw Error(`hook '${n}' does not exist`);
					if (n === "options") continue;
					let r = n, i = e.hooks[r], a = t[r];
					t[r] = K.passThroughHooks.has(n) ? (e) => {
						if (this.defaults.async) return Promise.resolve(i.call(t, e)).then((e) => a.call(t, e));
						let n = i.call(t, e);
						return a.call(t, n);
					} : (...e) => {
						let n = i.apply(t, e);
						return n === !1 && (n = a.apply(t, e)), n;
					};
				}
				n.hooks = t;
			}
			if (e.walkTokens) {
				let t = this.defaults.walkTokens, r = e.walkTokens;
				n.walkTokens = function(e) {
					let n = [];
					return n.push(r.call(this, e)), t && (n = n.concat(t.call(this, e))), n;
				};
			}
			this.defaults = {
				...this.defaults,
				...n
			};
		}), this;
	}
	setOptions(e) {
		return this.defaults = {
			...this.defaults,
			...e
		}, this;
	}
	lexer(e, t) {
		return H.lex(e, t ?? this.defaults);
	}
	parser(e, t) {
		return G.parse(e, t ?? this.defaults);
	}
	#e(e, t) {
		return (n, r) => {
			let i = { ...r }, a = {
				...this.defaults,
				...i
			};
			this.defaults.async === !0 && i.async === !1 && (a.silent || console.warn("marked(): The async option was set to true by an extension. The async: false option sent to parse will be ignored."), a.async = !0);
			let o = this.#t(!!a.silent, !!a.async);
			if (n == null) return o(/* @__PURE__ */ Error("marked(): input parameter is undefined or null"));
			if (typeof n != "string") return o(/* @__PURE__ */ Error("marked(): input parameter is of type " + Object.prototype.toString.call(n) + ", string expected"));
			if (a.hooks && (a.hooks.options = a), a.async) return Promise.resolve(a.hooks ? a.hooks.preprocess(n) : n).then((t) => e(t, a)).then((e) => a.hooks ? a.hooks.processAllTokens(e) : e).then((e) => a.walkTokens ? Promise.all(this.walkTokens(e, a.walkTokens)).then(() => e) : e).then((e) => t(e, a)).then((e) => a.hooks ? a.hooks.postprocess(e) : e).catch(o);
			try {
				a.hooks && (n = a.hooks.preprocess(n));
				let r = e(n, a);
				a.hooks && (r = a.hooks.processAllTokens(r)), a.walkTokens && this.walkTokens(r, a.walkTokens);
				let i = t(r, a);
				return a.hooks && (i = a.hooks.postprocess(i)), i;
			} catch (e) {
				return o(e);
			}
		};
	}
	#t(e, t) {
		return (n) => {
			if (n.message += "\nPlease report this to https://github.com/markedjs/marked.", e) {
				let e = "<p>An error occurred:</p><pre>" + l(n.message + "", !0) + "</pre>";
				return t ? Promise.resolve(e) : e;
			}
			if (t) return Promise.reject(n);
			throw n;
		};
	}
}();
function J(e, t) {
	return q.parse(e, t);
}
J.options = J.setOptions = function(e) {
	return q.setOptions(e), J.defaults = q.defaults, n(J.defaults), J;
}, J.getDefaults = e, J.defaults = t, J.use = function(...e) {
	return q.use(...e), J.defaults = q.defaults, n(J.defaults), J;
}, J.walkTokens = function(e, t) {
	return q.walkTokens(e, t);
}, J.parseInline = q.parseInline, J.Parser = G, J.parser = G.parse, J.Renderer = U, J.TextRenderer = W, J.Lexer = H, J.lexer = H.lex, J.Tokenizer = x, J.Hooks = K, J.parse = J, J.options, J.setOptions, J.use, J.walkTokens, J.parseInline, G.parse, H.lex;
//#endregion
//#region node_modules/@kingsoftcloud/ksadk-web/dist-lib/capabilities-CxHZ869f.js
var Y = ["responses", "chat_completions"], we = /* @__PURE__ */ new Map([["openclaw", "OpenClaw"], ["hermes", "Hermes"]]);
function Te(e) {
	return String(e || "").trim().toLowerCase();
}
function Ee(...e) {
	for (let t of e) {
		let e = Te(t);
		if (e) return e;
	}
	return "";
}
function X(e) {
	return e && typeof e == "object" && !Array.isArray(e) ? e : {};
}
function Z(e) {
	if (!Array.isArray(e)) return Y;
	let t = e.filter((e) => e === "responses" || e === "chat_completions");
	return t.length > 0 ? t : Y;
}
function Q(e, t) {
	return typeof e == "boolean" ? e : t;
}
function De(e) {
	return Array.isArray(e) ? e.map((e) => X(e)).filter((e) => typeof e.name == "string" && e.name.trim()).map((e) => {
		let t = {
			...e,
			name: e.name.trim(),
			group: typeof e.group == "string" ? e.group : "",
			risk_level: typeof e.risk_level == "string" ? e.risk_level : "low",
			requires_approval: !!e.requires_approval,
			side_effects: Array.isArray(e.side_effects) ? e.side_effects.filter((e) => typeof e == "string") : [],
			enabled: Q(e.enabled, !0)
		};
		return typeof e.description == "string" && (t.description = e.description), t;
	}) : [];
}
function Oe(e) {
	let t = X(e), n = Array.isArray(t.Modes) ? t.Modes.filter((e) => [
		"ask",
		"risk",
		"full"
	].includes(e)) : [
		"ask",
		"risk",
		"full"
	], r = n.length > 0 ? [...new Set(n)] : [
		"ask",
		"risk",
		"full"
	];
	return {
		Modes: r,
		DefaultMode: r.includes(t.DefaultMode) ? t.DefaultMode : "risk",
		RuntimeOverride: Q(t.RuntimeOverride, !0)
	};
}
function ke(e) {
	return Array.isArray(e) ? e.flatMap((e) => {
		let t = X(e), n = String(t.Protocol || "").trim().toLowerCase(), r = String(t.Endpoint || "").trim();
		if (!["ag-ui", "responses"].includes(n) || !r.startsWith("/")) return [];
		let i = X(t.Capabilities);
		return [{
			Protocol: n,
			Runtime: String(t.Runtime || "").trim(),
			Endpoint: r,
			Version: String(t.Version || "").trim(),
			Capabilities: {
				A2UI: !!i.A2UI,
				Interrupt: !!i.Interrupt,
				Cancel: !!i.Cancel
			}
		}];
	}) : [];
}
function Ae(e) {
	let t = e?.Data || e || {}, n = X(t.Capabilities), r = X(t.HostedRuntime), i = Ee(t.Agent?.Framework, r.Framework, r.Type), a = we.get(i) || "", o = Z(t.ApiFormats || n.HostedChat?.ApiFormats), s = i !== "openclaw", c = !!a, l = !!a, u = X(n.HostedChat), d = X(n.NativeDashboard), f = X(n.NativeTerminal), p = X(n.RunLifecycle), m = X(t.HostedChat), h = ke(u.Transports || m.Transports), g = String(u.PreferredTransport || m.PreferredTransport || "responses").trim().toLowerCase(), _ = h.some((e) => e.Protocol === g) ? g : "responses", v = Q(u.Enabled, s), y = Q(d.Enabled, c), b = Q(f.Enabled, l), x = Q(p.Enabled, v);
	return {
		...n,
		HostedChat: {
			Enabled: v,
			ApiFormats: Z(u.ApiFormats || o),
			PreferredTransport: _,
			Transports: h
		},
		NativeDashboard: {
			Enabled: y,
			Href: typeof d.Href == "string" ? d.Href : y ? "/" : null,
			Label: typeof d.Label == "string" ? d.Label : y ? "管理平台" : null
		},
		NativeTerminal: {
			Enabled: b,
			Mode: typeof f.Mode == "string" ? f.Mode : b ? "tui" : null,
			Protocol: typeof f.Protocol == "string" ? f.Protocol : "ks-terminal.v1",
			Path: typeof f.Path == "string" ? f.Path : "/_ksadk/terminal/ws"
		},
		RunLifecycle: {
			Enabled: x,
			Resume: Q(p.Resume, x),
			Abort: Q(p.Abort, x),
			Checkpoints: Q(p.Checkpoints, !1),
			CheckpointResume: Q(p.CheckpointResume, !1),
			CheckpointResumePreview: Q(p.CheckpointResumePreview, !1)
		},
		ApprovalPolicy: Oe(n.ApprovalPolicy),
		BuiltinTools: De(n.BuiltinTools)
	};
}
//#endregion
//#region src/shared-message.ts
function je(e) {
	let t = String(e || "").replace(/\r\n?/g, "\n").trim();
	return (t.match(/```/g)?.length || 0) % 2 == 1 ? `${t}\n\`\`\`` : t;
}
var Me = "\n  :host { display: block; color: inherit; font: inherit; }\n  * { box-sizing: border-box; }\n  .markdown { max-width: 100%; color: inherit; font: inherit; line-height: 1.72; overflow-wrap: anywhere; }\n  .markdown > :first-child { margin-top: 0; }\n  .markdown > :last-child { margin-bottom: 0; }\n  h1, h2, h3 { margin: 1.2em 0 .5em; color: inherit; line-height: 1.35; }\n  h1 { font-size: 1.35em; }\n  h2 { font-size: 1.2em; }\n  h3 { font-size: 1.08em; }\n  p { margin: .62em 0; }\n  ul, ol { margin: .62em 0; padding-left: 1.45em; }\n  li { margin: .28em 0; }\n  strong { font-weight: 650; }\n  a { color: #2563eb; text-decoration: none; }\n  a:hover { text-decoration: underline; }\n  blockquote { margin: .75em 0; padding-left: 1em; border-left: 2px solid #cbd5e1; color: #64748b; }\n  code { padding: .12em .35em; border: 1px solid #e2e8f0; border-radius: 5px; background: #f8fafc; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }\n  pre { max-width: 100%; overflow: auto; padding: 12px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }\n  pre code { padding: 0; border: 0; background: transparent; }\n  table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; }\n  th, td { padding: 7px 9px; border-bottom: 1px solid #e2e8f0; text-align: left; }\n", Ne = /* @__PURE__ */ new Set([
	"A",
	"BLOCKQUOTE",
	"BR",
	"CODE",
	"DEL",
	"DIV",
	"EM",
	"H1",
	"H2",
	"H3",
	"H4",
	"HR",
	"LI",
	"OL",
	"P",
	"PRE",
	"SPAN",
	"STRONG",
	"TABLE",
	"TBODY",
	"TD",
	"TH",
	"THEAD",
	"TR",
	"UL"
]);
function Pe(e) {
	let t = document.createElement("template");
	t.innerHTML = e;
	for (let e of Array.from(t.content.querySelectorAll("*"))) {
		if (!Ne.has(e.tagName)) {
			e.replaceWith(...Array.from(e.childNodes));
			continue;
		}
		for (let t of Array.from(e.attributes)) (e.tagName !== "A" || t.name !== "href") && e.removeAttribute(t.name);
		if (e.tagName === "A") {
			let t = e;
			/^(https?:|mailto:)/i.test(t.getAttribute("href") || "") || t.removeAttribute("href"), t.target = "_blank", t.rel = "noopener noreferrer";
		}
	}
	return t.content;
}
var $ = class extends HTMLElement {
	value = "";
	mount;
	constructor() {
		super();
		let e = this.attachShadow({ mode: "open" }), t = document.createElement("style");
		t.textContent = Me, this.mount = document.createElement("div"), this.mount.className = "markdown", e.append(t, this.mount);
	}
	connectedCallback() {
		this.renderContent();
	}
	set content(e) {
		this.value = String(e ?? ""), this.renderContent();
	}
	get content() {
		return this.value;
	}
	renderContent() {
		let e = je(this.value), t = J.parse(e, {
			async: !1,
			gfm: !0,
			breaks: !0
		});
		this.mount.replaceChildren(Pe(String(t)));
	}
};
customElements.get("ksadk-message") || customElements.define("ksadk-message", $), Object.assign(window, { AgentKitSharedWeb: Object.freeze({ normalizeCapabilities: Ae }) });
//#endregion
export { $ as KsadkMessageElement };
