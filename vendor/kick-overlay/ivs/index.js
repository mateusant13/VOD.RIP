var IVSPlayerModule = (() => {
  var __getOwnPropNames = Object.getOwnPropertyNames;
  var __commonJS = (cb, mod) => function __require() {
    try {
      return mod || (0, cb[__getOwnPropNames(cb)[0]])((mod = { exports: {} }).exports, mod), mod.exports;
    } catch (e) {
      throw mod = 0, e;
    }
  };

  // /tmp/ivs-build/node_modules/bowser/es5.js
  var require_es5 = __commonJS({
    "/tmp/ivs-build/node_modules/bowser/es5.js"(exports, module) {
      !(function(e, t) {
        "object" == typeof exports && "object" == typeof module ? module.exports = t() : "function" == typeof define && define.amd ? define([], t) : "object" == typeof exports ? exports.bowser = t() : e.bowser = t();
      })(exports, (function() {
        return (function(e) {
          var t = {};
          function r(i) {
            if (t[i]) return t[i].exports;
            var n = t[i] = { i, l: false, exports: {} };
            return e[i].call(n.exports, n, n.exports, r), n.l = true, n.exports;
          }
          return r.m = e, r.c = t, r.d = function(e2, t2, i) {
            r.o(e2, t2) || Object.defineProperty(e2, t2, { enumerable: true, get: i });
          }, r.r = function(e2) {
            "undefined" != typeof Symbol && Symbol.toStringTag && Object.defineProperty(e2, Symbol.toStringTag, { value: "Module" }), Object.defineProperty(e2, "__esModule", { value: true });
          }, r.t = function(e2, t2) {
            if (1 & t2 && (e2 = r(e2)), 8 & t2) return e2;
            if (4 & t2 && "object" == typeof e2 && e2 && e2.__esModule) return e2;
            var i = /* @__PURE__ */ Object.create(null);
            if (r.r(i), Object.defineProperty(i, "default", { enumerable: true, value: e2 }), 2 & t2 && "string" != typeof e2) for (var n in e2) r.d(i, n, function(t3) {
              return e2[t3];
            }.bind(null, n));
            return i;
          }, r.n = function(e2) {
            var t2 = e2 && e2.__esModule ? function() {
              return e2.default;
            } : function() {
              return e2;
            };
            return r.d(t2, "a", t2), t2;
          }, r.o = function(e2, t2) {
            return Object.prototype.hasOwnProperty.call(e2, t2);
          }, r.p = "", r(r.s = 90);
        })({ 17: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i = r(18), n = (function() {
            function e2() {
            }
            return e2.getFirstMatch = function(e3, t2) {
              var r2 = t2.match(e3);
              return r2 && r2.length > 0 && r2[1] || "";
            }, e2.getSecondMatch = function(e3, t2) {
              var r2 = t2.match(e3);
              return r2 && r2.length > 1 && r2[2] || "";
            }, e2.matchAndReturnConst = function(e3, t2, r2) {
              if (e3.test(t2)) return r2;
            }, e2.getWindowsVersionName = function(e3) {
              switch (e3) {
                case "NT":
                  return "NT";
                case "XP":
                  return "XP";
                case "NT 5.0":
                  return "2000";
                case "NT 5.1":
                  return "XP";
                case "NT 5.2":
                  return "2003";
                case "NT 6.0":
                  return "Vista";
                case "NT 6.1":
                  return "7";
                case "NT 6.2":
                  return "8";
                case "NT 6.3":
                  return "8.1";
                case "NT 10.0":
                  return "10";
                default:
                  return;
              }
            }, e2.getMacOSVersionName = function(e3) {
              var t2 = e3.split(".").splice(0, 2).map((function(e4) {
                return parseInt(e4, 10) || 0;
              }));
              t2.push(0);
              var r2 = t2[0], i2 = t2[1];
              if (10 === r2) switch (i2) {
                case 5:
                  return "Leopard";
                case 6:
                  return "Snow Leopard";
                case 7:
                  return "Lion";
                case 8:
                  return "Mountain Lion";
                case 9:
                  return "Mavericks";
                case 10:
                  return "Yosemite";
                case 11:
                  return "El Capitan";
                case 12:
                  return "Sierra";
                case 13:
                  return "High Sierra";
                case 14:
                  return "Mojave";
                case 15:
                  return "Catalina";
                default:
                  return;
              }
              switch (r2) {
                case 11:
                  return "Big Sur";
                case 12:
                  return "Monterey";
                case 13:
                  return "Ventura";
                case 14:
                  return "Sonoma";
                case 15:
                  return "Sequoia";
                default:
                  return;
              }
            }, e2.getAndroidVersionName = function(e3) {
              var t2 = e3.split(".").splice(0, 2).map((function(e4) {
                return parseInt(e4, 10) || 0;
              }));
              if (t2.push(0), !(1 === t2[0] && t2[1] < 5)) return 1 === t2[0] && t2[1] < 6 ? "Cupcake" : 1 === t2[0] && t2[1] >= 6 ? "Donut" : 2 === t2[0] && t2[1] < 2 ? "Eclair" : 2 === t2[0] && 2 === t2[1] ? "Froyo" : 2 === t2[0] && t2[1] > 2 ? "Gingerbread" : 3 === t2[0] ? "Honeycomb" : 4 === t2[0] && t2[1] < 1 ? "Ice Cream Sandwich" : 4 === t2[0] && t2[1] < 4 ? "Jelly Bean" : 4 === t2[0] && t2[1] >= 4 ? "KitKat" : 5 === t2[0] ? "Lollipop" : 6 === t2[0] ? "Marshmallow" : 7 === t2[0] ? "Nougat" : 8 === t2[0] ? "Oreo" : 9 === t2[0] ? "Pie" : void 0;
            }, e2.getVersionPrecision = function(e3) {
              return e3.split(".").length;
            }, e2.compareVersions = function(t2, r2, i2) {
              void 0 === i2 && (i2 = false);
              var n2 = e2.getVersionPrecision(t2), a = e2.getVersionPrecision(r2), o = Math.max(n2, a), s = 0, u = e2.map([t2, r2], (function(t3) {
                var r3 = o - e2.getVersionPrecision(t3), i3 = t3 + new Array(r3 + 1).join(".0");
                return e2.map(i3.split("."), (function(e3) {
                  return new Array(20 - e3.length).join("0") + e3;
                })).reverse();
              }));
              for (i2 && (s = o - Math.min(n2, a)), o -= 1; o >= s; ) {
                if (u[0][o] > u[1][o]) return 1;
                if (u[0][o] === u[1][o]) {
                  if (o === s) return 0;
                  o -= 1;
                } else if (u[0][o] < u[1][o]) return -1;
              }
            }, e2.map = function(e3, t2) {
              var r2, i2 = [];
              if (Array.prototype.map) return Array.prototype.map.call(e3, t2);
              for (r2 = 0; r2 < e3.length; r2 += 1) i2.push(t2(e3[r2]));
              return i2;
            }, e2.find = function(e3, t2) {
              var r2, i2;
              if (Array.prototype.find) return Array.prototype.find.call(e3, t2);
              for (r2 = 0, i2 = e3.length; r2 < i2; r2 += 1) {
                var n2 = e3[r2];
                if (t2(n2, r2)) return n2;
              }
            }, e2.assign = function(e3) {
              for (var t2, r2, i2 = e3, n2 = arguments.length, a = new Array(n2 > 1 ? n2 - 1 : 0), o = 1; o < n2; o++) a[o - 1] = arguments[o];
              if (Object.assign) return Object.assign.apply(Object, [e3].concat(a));
              var s = function() {
                var e4 = a[t2];
                "object" == typeof e4 && null !== e4 && Object.keys(e4).forEach((function(t3) {
                  i2[t3] = e4[t3];
                }));
              };
              for (t2 = 0, r2 = a.length; t2 < r2; t2 += 1) s();
              return e3;
            }, e2.getBrowserAlias = function(e3) {
              return i.BROWSER_ALIASES_MAP[e3];
            }, e2.getBrowserTypeByAlias = function(e3) {
              return i.BROWSER_MAP[e3] || "";
            }, e2;
          })();
          t.default = n, e.exports = t.default;
        }, 18: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.ENGINE_MAP = t.OS_MAP = t.PLATFORMS_MAP = t.BROWSER_MAP = t.BROWSER_ALIASES_MAP = void 0;
          t.BROWSER_ALIASES_MAP = { AmazonBot: "amazonbot", "Amazon Silk": "amazon_silk", "Android Browser": "android", BaiduSpider: "baiduspider", Bada: "bada", BingCrawler: "bingcrawler", Brave: "brave", BlackBerry: "blackberry", "ChatGPT-User": "chatgpt_user", Chrome: "chrome", ClaudeBot: "claudebot", Chromium: "chromium", Diffbot: "diffbot", DuckDuckBot: "duckduckbot", DuckDuckGo: "duckduckgo", Electron: "electron", Epiphany: "epiphany", FacebookExternalHit: "facebookexternalhit", Firefox: "firefox", Focus: "focus", Generic: "generic", "Google Search": "google_search", Googlebot: "googlebot", GPTBot: "gptbot", "Internet Explorer": "ie", InternetArchiveCrawler: "internetarchivecrawler", "K-Meleon": "k_meleon", LibreWolf: "librewolf", Linespider: "linespider", Maxthon: "maxthon", "Meta-ExternalAds": "meta_externalads", "Meta-ExternalAgent": "meta_externalagent", "Meta-ExternalFetcher": "meta_externalfetcher", "Meta-WebIndexer": "meta_webindexer", "Microsoft Edge": "edge", "MZ Browser": "mz", "NAVER Whale Browser": "naver", "OAI-SearchBot": "oai_searchbot", Omgilibot: "omgilibot", Opera: "opera", "Opera Coast": "opera_coast", "Pale Moon": "pale_moon", PerplexityBot: "perplexitybot", "Perplexity-User": "perplexity_user", PhantomJS: "phantomjs", PingdomBot: "pingdombot", Puffin: "puffin", QQ: "qq", QQLite: "qqlite", QupZilla: "qupzilla", Roku: "roku", Safari: "safari", Sailfish: "sailfish", "Samsung Internet for Android": "samsung_internet", SlackBot: "slackbot", SeaMonkey: "seamonkey", Sleipnir: "sleipnir", "Sogou Browser": "sogou", Swing: "swing", Tizen: "tizen", "UC Browser": "uc", Vivaldi: "vivaldi", "WebOS Browser": "webos", WeChat: "wechat", YahooSlurp: "yahooslurp", "Yandex Browser": "yandex", YandexBot: "yandexbot", YouBot: "youbot" };
          t.BROWSER_MAP = { amazonbot: "AmazonBot", amazon_silk: "Amazon Silk", android: "Android Browser", baiduspider: "BaiduSpider", bada: "Bada", bingcrawler: "BingCrawler", blackberry: "BlackBerry", brave: "Brave", chatgpt_user: "ChatGPT-User", chrome: "Chrome", claudebot: "ClaudeBot", chromium: "Chromium", diffbot: "Diffbot", duckduckbot: "DuckDuckBot", duckduckgo: "DuckDuckGo", edge: "Microsoft Edge", electron: "Electron", epiphany: "Epiphany", facebookexternalhit: "FacebookExternalHit", firefox: "Firefox", focus: "Focus", generic: "Generic", google_search: "Google Search", googlebot: "Googlebot", gptbot: "GPTBot", ie: "Internet Explorer", internetarchivecrawler: "InternetArchiveCrawler", k_meleon: "K-Meleon", librewolf: "LibreWolf", linespider: "Linespider", maxthon: "Maxthon", meta_externalads: "Meta-ExternalAds", meta_externalagent: "Meta-ExternalAgent", meta_externalfetcher: "Meta-ExternalFetcher", meta_webindexer: "Meta-WebIndexer", mz: "MZ Browser", naver: "NAVER Whale Browser", oai_searchbot: "OAI-SearchBot", omgilibot: "Omgilibot", opera: "Opera", opera_coast: "Opera Coast", pale_moon: "Pale Moon", perplexitybot: "PerplexityBot", perplexity_user: "Perplexity-User", phantomjs: "PhantomJS", pingdombot: "PingdomBot", puffin: "Puffin", qq: "QQ Browser", qqlite: "QQ Browser Lite", qupzilla: "QupZilla", roku: "Roku", safari: "Safari", sailfish: "Sailfish", samsung_internet: "Samsung Internet for Android", seamonkey: "SeaMonkey", slackbot: "SlackBot", sleipnir: "Sleipnir", sogou: "Sogou Browser", swing: "Swing", tizen: "Tizen", uc: "UC Browser", vivaldi: "Vivaldi", webos: "WebOS Browser", wechat: "WeChat", yahooslurp: "YahooSlurp", yandex: "Yandex Browser", yandexbot: "YandexBot", youbot: "YouBot" };
          t.PLATFORMS_MAP = { bot: "bot", desktop: "desktop", mobile: "mobile", tablet: "tablet", tv: "tv" };
          t.OS_MAP = { Android: "Android", Bada: "Bada", BlackBerry: "BlackBerry", ChromeOS: "Chrome OS", HarmonyOS: "HarmonyOS", iOS: "iOS", Linux: "Linux", MacOS: "macOS", PlayStation4: "PlayStation 4", Roku: "Roku", Tizen: "Tizen", WebOS: "WebOS", Windows: "Windows", WindowsPhone: "Windows Phone" };
          t.ENGINE_MAP = { Blink: "Blink", EdgeHTML: "EdgeHTML", Gecko: "Gecko", Presto: "Presto", Trident: "Trident", WebKit: "WebKit" };
        }, 90: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i, n = (i = r(91)) && i.__esModule ? i : { default: i }, a = r(18);
          function o(e2, t2) {
            for (var r2 = 0; r2 < t2.length; r2++) {
              var i2 = t2[r2];
              i2.enumerable = i2.enumerable || false, i2.configurable = true, "value" in i2 && (i2.writable = true), Object.defineProperty(e2, i2.key, i2);
            }
          }
          var s = (function() {
            function e2() {
            }
            var t2, r2, i2;
            return e2.getParser = function(e3, t3, r3) {
              if (void 0 === t3 && (t3 = false), void 0 === r3 && (r3 = null), "string" != typeof e3) throw new Error("UserAgent should be a string");
              return new n.default(e3, t3, r3);
            }, e2.parse = function(e3, t3) {
              return void 0 === t3 && (t3 = null), new n.default(e3, t3).getResult();
            }, t2 = e2, i2 = [{ key: "BROWSER_MAP", get: function() {
              return a.BROWSER_MAP;
            } }, { key: "ENGINE_MAP", get: function() {
              return a.ENGINE_MAP;
            } }, { key: "OS_MAP", get: function() {
              return a.OS_MAP;
            } }, { key: "PLATFORMS_MAP", get: function() {
              return a.PLATFORMS_MAP;
            } }], (r2 = null) && o(t2.prototype, r2), i2 && o(t2, i2), e2;
          })();
          t.default = s, e.exports = t.default;
        }, 91: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i = u(r(92)), n = u(r(93)), a = u(r(94)), o = u(r(95)), s = u(r(17));
          function u(e2) {
            return e2 && e2.__esModule ? e2 : { default: e2 };
          }
          var d = (function() {
            function e2(e3, t3, r2) {
              if (void 0 === t3 && (t3 = false), void 0 === r2 && (r2 = null), null == e3 || "" === e3) throw new Error("UserAgent parameter can't be empty");
              this._ua = e3;
              var i2 = false;
              "boolean" == typeof t3 ? (i2 = t3, this._hints = r2) : this._hints = null != t3 && "object" == typeof t3 ? t3 : null, this.parsedResult = {}, true !== i2 && this.parse();
            }
            var t2 = e2.prototype;
            return t2.getHints = function() {
              return this._hints;
            }, t2.hasBrand = function(e3) {
              if (!this._hints || !Array.isArray(this._hints.brands)) return false;
              var t3 = e3.toLowerCase();
              return this._hints.brands.some((function(e4) {
                return e4.brand && e4.brand.toLowerCase() === t3;
              }));
            }, t2.getBrandVersion = function(e3) {
              if (this._hints && Array.isArray(this._hints.brands)) {
                var t3 = e3.toLowerCase(), r2 = this._hints.brands.find((function(e4) {
                  return e4.brand && e4.brand.toLowerCase() === t3;
                }));
                return r2 ? r2.version : void 0;
              }
            }, t2.getUA = function() {
              return this._ua;
            }, t2.test = function(e3) {
              return e3.test(this._ua);
            }, t2.parseBrowser = function() {
              var e3 = this;
              this.parsedResult.browser = {};
              var t3 = s.default.find(i.default, (function(t4) {
                if ("function" == typeof t4.test) return t4.test(e3);
                if (Array.isArray(t4.test)) return t4.test.some((function(t5) {
                  return e3.test(t5);
                }));
                throw new Error("Browser's test function is not valid");
              }));
              return t3 && (this.parsedResult.browser = t3.describe(this.getUA(), this)), this.parsedResult.browser;
            }, t2.getBrowser = function() {
              return this.parsedResult.browser ? this.parsedResult.browser : this.parseBrowser();
            }, t2.getBrowserName = function(e3) {
              return e3 ? String(this.getBrowser().name).toLowerCase() || "" : this.getBrowser().name || "";
            }, t2.getBrowserVersion = function() {
              return this.getBrowser().version;
            }, t2.getOS = function() {
              return this.parsedResult.os ? this.parsedResult.os : this.parseOS();
            }, t2.parseOS = function() {
              var e3 = this;
              this.parsedResult.os = {};
              var t3 = s.default.find(n.default, (function(t4) {
                if ("function" == typeof t4.test) return t4.test(e3);
                if (Array.isArray(t4.test)) return t4.test.some((function(t5) {
                  return e3.test(t5);
                }));
                throw new Error("Browser's test function is not valid");
              }));
              return t3 && (this.parsedResult.os = t3.describe(this.getUA())), this.parsedResult.os;
            }, t2.getOSName = function(e3) {
              var t3 = this.getOS().name;
              return e3 ? String(t3).toLowerCase() || "" : t3 || "";
            }, t2.getOSVersion = function() {
              return this.getOS().version;
            }, t2.getPlatform = function() {
              return this.parsedResult.platform ? this.parsedResult.platform : this.parsePlatform();
            }, t2.getPlatformType = function(e3) {
              void 0 === e3 && (e3 = false);
              var t3 = this.getPlatform().type;
              return e3 ? String(t3).toLowerCase() || "" : t3 || "";
            }, t2.parsePlatform = function() {
              var e3 = this;
              this.parsedResult.platform = {};
              var t3 = s.default.find(a.default, (function(t4) {
                if ("function" == typeof t4.test) return t4.test(e3);
                if (Array.isArray(t4.test)) return t4.test.some((function(t5) {
                  return e3.test(t5);
                }));
                throw new Error("Browser's test function is not valid");
              }));
              return t3 && (this.parsedResult.platform = t3.describe(this.getUA())), this.parsedResult.platform;
            }, t2.getEngine = function() {
              return this.parsedResult.engine ? this.parsedResult.engine : this.parseEngine();
            }, t2.getEngineName = function(e3) {
              return e3 ? String(this.getEngine().name).toLowerCase() || "" : this.getEngine().name || "";
            }, t2.parseEngine = function() {
              var e3 = this;
              this.parsedResult.engine = {};
              var t3 = s.default.find(o.default, (function(t4) {
                if ("function" == typeof t4.test) return t4.test(e3);
                if (Array.isArray(t4.test)) return t4.test.some((function(t5) {
                  return e3.test(t5);
                }));
                throw new Error("Browser's test function is not valid");
              }));
              return t3 && (this.parsedResult.engine = t3.describe(this.getUA())), this.parsedResult.engine;
            }, t2.parse = function() {
              return this.parseBrowser(), this.parseOS(), this.parsePlatform(), this.parseEngine(), this;
            }, t2.getResult = function() {
              return s.default.assign({}, this.parsedResult);
            }, t2.satisfies = function(e3) {
              var t3 = this, r2 = {}, i2 = 0, n2 = {}, a2 = 0;
              if (Object.keys(e3).forEach((function(t4) {
                var o3 = e3[t4];
                "string" == typeof o3 ? (n2[t4] = o3, a2 += 1) : "object" == typeof o3 && (r2[t4] = o3, i2 += 1);
              })), i2 > 0) {
                var o2 = Object.keys(r2), u2 = s.default.find(o2, (function(e4) {
                  return t3.isOS(e4);
                }));
                if (u2) {
                  var d2 = this.satisfies(r2[u2]);
                  if (void 0 !== d2) return d2;
                }
                var c = s.default.find(o2, (function(e4) {
                  return t3.isPlatform(e4);
                }));
                if (c) {
                  var f = this.satisfies(r2[c]);
                  if (void 0 !== f) return f;
                }
              }
              if (a2 > 0) {
                var l = Object.keys(n2), b = s.default.find(l, (function(e4) {
                  return t3.isBrowser(e4, true);
                }));
                if (void 0 !== b) return this.compareVersion(n2[b]);
              }
            }, t2.isBrowser = function(e3, t3) {
              void 0 === t3 && (t3 = false);
              var r2 = this.getBrowserName().toLowerCase(), i2 = e3.toLowerCase(), n2 = s.default.getBrowserTypeByAlias(i2);
              return t3 && n2 && (i2 = n2.toLowerCase()), i2 === r2;
            }, t2.compareVersion = function(e3) {
              var t3 = [0], r2 = e3, i2 = false, n2 = this.getBrowserVersion();
              if ("string" == typeof n2) return ">" === e3[0] || "<" === e3[0] ? (r2 = e3.substr(1), "=" === e3[1] ? (i2 = true, r2 = e3.substr(2)) : t3 = [], ">" === e3[0] ? t3.push(1) : t3.push(-1)) : "=" === e3[0] ? r2 = e3.substr(1) : "~" === e3[0] && (i2 = true, r2 = e3.substr(1)), t3.indexOf(s.default.compareVersions(n2, r2, i2)) > -1;
            }, t2.isOS = function(e3) {
              return this.getOSName(true) === String(e3).toLowerCase();
            }, t2.isPlatform = function(e3) {
              return this.getPlatformType(true) === String(e3).toLowerCase();
            }, t2.isEngine = function(e3) {
              return this.getEngineName(true) === String(e3).toLowerCase();
            }, t2.is = function(e3, t3) {
              return void 0 === t3 && (t3 = false), this.isBrowser(e3, t3) || this.isOS(e3) || this.isPlatform(e3);
            }, t2.some = function(e3) {
              var t3 = this;
              return void 0 === e3 && (e3 = []), e3.some((function(e4) {
                return t3.is(e4);
              }));
            }, e2;
          })();
          t.default = d, e.exports = t.default;
        }, 92: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i, n = (i = r(17)) && i.__esModule ? i : { default: i };
          var a = /version\/(\d+(\.?_?\d+)+)/i, o = [{ test: [/gptbot/i], describe: function(e2) {
            var t2 = { name: "GPTBot" }, r2 = n.default.getFirstMatch(/gptbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/chatgpt-user/i], describe: function(e2) {
            var t2 = { name: "ChatGPT-User" }, r2 = n.default.getFirstMatch(/chatgpt-user\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/oai-searchbot/i], describe: function(e2) {
            var t2 = { name: "OAI-SearchBot" }, r2 = n.default.getFirstMatch(/oai-searchbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/claudebot/i, /claude-web/i, /claude-user/i, /claude-searchbot/i], describe: function(e2) {
            var t2 = { name: "ClaudeBot" }, r2 = n.default.getFirstMatch(/(?:claudebot|claude-web|claude-user|claude-searchbot)\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/omgilibot/i, /webzio-extended/i], describe: function(e2) {
            var t2 = { name: "Omgilibot" }, r2 = n.default.getFirstMatch(/(?:omgilibot|webzio-extended)\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/diffbot/i], describe: function(e2) {
            var t2 = { name: "Diffbot" }, r2 = n.default.getFirstMatch(/diffbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/perplexitybot/i], describe: function(e2) {
            var t2 = { name: "PerplexityBot" }, r2 = n.default.getFirstMatch(/perplexitybot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/perplexity-user/i], describe: function(e2) {
            var t2 = { name: "Perplexity-User" }, r2 = n.default.getFirstMatch(/perplexity-user\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/youbot/i], describe: function(e2) {
            var t2 = { name: "YouBot" }, r2 = n.default.getFirstMatch(/youbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/meta-webindexer/i], describe: function(e2) {
            var t2 = { name: "Meta-WebIndexer" }, r2 = n.default.getFirstMatch(/meta-webindexer\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/meta-externalads/i], describe: function(e2) {
            var t2 = { name: "Meta-ExternalAds" }, r2 = n.default.getFirstMatch(/meta-externalads\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/meta-externalagent/i], describe: function(e2) {
            var t2 = { name: "Meta-ExternalAgent" }, r2 = n.default.getFirstMatch(/meta-externalagent\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/meta-externalfetcher/i], describe: function(e2) {
            var t2 = { name: "Meta-ExternalFetcher" }, r2 = n.default.getFirstMatch(/meta-externalfetcher\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/googlebot/i], describe: function(e2) {
            var t2 = { name: "Googlebot" }, r2 = n.default.getFirstMatch(/googlebot\/(\d+(\.\d+))/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/linespider/i], describe: function(e2) {
            var t2 = { name: "Linespider" }, r2 = n.default.getFirstMatch(/(?:linespider)(?:-[-\w]+)?[\s/](\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/amazonbot/i], describe: function(e2) {
            var t2 = { name: "AmazonBot" }, r2 = n.default.getFirstMatch(/amazonbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/bingbot/i], describe: function(e2) {
            var t2 = { name: "BingCrawler" }, r2 = n.default.getFirstMatch(/bingbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/baiduspider/i], describe: function(e2) {
            var t2 = { name: "BaiduSpider" }, r2 = n.default.getFirstMatch(/baiduspider\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/duckduckbot/i], describe: function(e2) {
            var t2 = { name: "DuckDuckBot" }, r2 = n.default.getFirstMatch(/duckduckbot\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/ia_archiver/i], describe: function(e2) {
            var t2 = { name: "InternetArchiveCrawler" }, r2 = n.default.getFirstMatch(/ia_archiver\/(\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/facebookexternalhit/i, /facebookcatalog/i], describe: function() {
            return { name: "FacebookExternalHit" };
          } }, { test: [/slackbot/i, /slack-imgProxy/i], describe: function(e2) {
            var t2 = { name: "SlackBot" }, r2 = n.default.getFirstMatch(/(?:slackbot|slack-imgproxy)(?:-[-\w]+)?[\s/](\d+(\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/yahoo!?[\s/]*slurp/i], describe: function() {
            return { name: "YahooSlurp" };
          } }, { test: [/yandexbot/i, /yandexmobilebot/i], describe: function() {
            return { name: "YandexBot" };
          } }, { test: [/pingdom/i], describe: function() {
            return { name: "PingdomBot" };
          } }, { test: [/opera/i], describe: function(e2) {
            var t2 = { name: "Opera" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:opera)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/opr\/|opios/i], describe: function(e2) {
            var t2 = { name: "Opera" }, r2 = n.default.getFirstMatch(/(?:opr|opios)[\s/](\S+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/SamsungBrowser/i], describe: function(e2) {
            var t2 = { name: "Samsung Internet for Android" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:SamsungBrowser)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/Whale/i], describe: function(e2) {
            var t2 = { name: "NAVER Whale Browser" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:whale)[\s/](\d+(?:\.\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/PaleMoon/i], describe: function(e2) {
            var t2 = { name: "Pale Moon" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:PaleMoon)[\s/](\d+(?:\.\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/MZBrowser/i], describe: function(e2) {
            var t2 = { name: "MZ Browser" }, r2 = n.default.getFirstMatch(/(?:MZBrowser)[\s/](\d+(?:\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/focus/i], describe: function(e2) {
            var t2 = { name: "Focus" }, r2 = n.default.getFirstMatch(/(?:focus)[\s/](\d+(?:\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/swing/i], describe: function(e2) {
            var t2 = { name: "Swing" }, r2 = n.default.getFirstMatch(/(?:swing)[\s/](\d+(?:\.\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/coast/i], describe: function(e2) {
            var t2 = { name: "Opera Coast" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:coast)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/opt\/\d+(?:.?_?\d+)+/i], describe: function(e2) {
            var t2 = { name: "Opera Touch" }, r2 = n.default.getFirstMatch(/(?:opt)[\s/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/yabrowser/i], describe: function(e2) {
            var t2 = { name: "Yandex Browser" }, r2 = n.default.getFirstMatch(/(?:yabrowser)[\s/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/ucbrowser/i], describe: function(e2) {
            var t2 = { name: "UC Browser" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:ucbrowser)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/Maxthon|mxios/i], describe: function(e2) {
            var t2 = { name: "Maxthon" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:Maxthon|mxios)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/epiphany/i], describe: function(e2) {
            var t2 = { name: "Epiphany" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:epiphany)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/puffin/i], describe: function(e2) {
            var t2 = { name: "Puffin" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:puffin)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/sleipnir/i], describe: function(e2) {
            var t2 = { name: "Sleipnir" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:sleipnir)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/k-meleon/i], describe: function(e2) {
            var t2 = { name: "K-Meleon" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/(?:k-meleon)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/micromessenger/i], describe: function(e2) {
            var t2 = { name: "WeChat" }, r2 = n.default.getFirstMatch(/(?:micromessenger)[\s/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/qqbrowser/i], describe: function(e2) {
            var t2 = { name: /qqbrowserlite/i.test(e2) ? "QQ Browser Lite" : "QQ Browser" }, r2 = n.default.getFirstMatch(/(?:qqbrowserlite|qqbrowser)[/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/msie|trident/i], describe: function(e2) {
            var t2 = { name: "Internet Explorer" }, r2 = n.default.getFirstMatch(/(?:msie |rv:)(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/\sedg\//i], describe: function(e2) {
            var t2 = { name: "Microsoft Edge" }, r2 = n.default.getFirstMatch(/\sedg\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/edg([ea]|ios)/i], describe: function(e2) {
            var t2 = { name: "Microsoft Edge" }, r2 = n.default.getSecondMatch(/edg([ea]|ios)\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/vivaldi/i], describe: function(e2) {
            var t2 = { name: "Vivaldi" }, r2 = n.default.getFirstMatch(/vivaldi\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/seamonkey/i], describe: function(e2) {
            var t2 = { name: "SeaMonkey" }, r2 = n.default.getFirstMatch(/seamonkey\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/sailfish/i], describe: function(e2) {
            var t2 = { name: "Sailfish" }, r2 = n.default.getFirstMatch(/sailfish\s?browser\/(\d+(\.\d+)?)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/silk/i], describe: function(e2) {
            var t2 = { name: "Amazon Silk" }, r2 = n.default.getFirstMatch(/silk\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/phantom/i], describe: function(e2) {
            var t2 = { name: "PhantomJS" }, r2 = n.default.getFirstMatch(/phantomjs\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/slimerjs/i], describe: function(e2) {
            var t2 = { name: "SlimerJS" }, r2 = n.default.getFirstMatch(/slimerjs\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/blackberry|\bbb\d+/i, /rim\stablet/i], describe: function(e2) {
            var t2 = { name: "BlackBerry" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/blackberry[\d]+\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/(web|hpw)[o0]s/i], describe: function(e2) {
            var t2 = { name: "WebOS Browser" }, r2 = n.default.getFirstMatch(a, e2) || n.default.getFirstMatch(/w(?:eb)?[o0]sbrowser\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/bada/i], describe: function(e2) {
            var t2 = { name: "Bada" }, r2 = n.default.getFirstMatch(/dolfin\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/tizen/i], describe: function(e2) {
            var t2 = { name: "Tizen" }, r2 = n.default.getFirstMatch(/(?:tizen\s?)?browser\/(\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/qupzilla/i], describe: function(e2) {
            var t2 = { name: "QupZilla" }, r2 = n.default.getFirstMatch(/(?:qupzilla)[\s/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/librewolf/i], describe: function(e2) {
            var t2 = { name: "LibreWolf" }, r2 = n.default.getFirstMatch(/(?:librewolf)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/firefox|iceweasel|fxios/i], describe: function(e2) {
            var t2 = { name: "Firefox" }, r2 = n.default.getFirstMatch(/(?:firefox|iceweasel|fxios)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/electron/i], describe: function(e2) {
            var t2 = { name: "Electron" }, r2 = n.default.getFirstMatch(/(?:electron)\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/sogoumobilebrowser/i, /metasr/i, /se 2\.[x]/i], describe: function(e2) {
            var t2 = { name: "Sogou Browser" }, r2 = n.default.getFirstMatch(/(?:sogoumobilebrowser)[\s/](\d+(\.?_?\d+)+)/i, e2), i2 = n.default.getFirstMatch(/(?:chrome|crios|crmo)\/(\d+(\.?_?\d+)+)/i, e2), a2 = n.default.getFirstMatch(/se ([\d.]+)x/i, e2), o2 = r2 || i2 || a2;
            return o2 && (t2.version = o2), t2;
          } }, { test: [/MiuiBrowser/i], describe: function(e2) {
            var t2 = { name: "Miui" }, r2 = n.default.getFirstMatch(/(?:MiuiBrowser)[\s/](\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: function(e2) {
            return !!e2.hasBrand("DuckDuckGo") || e2.test(/\sDdg\/[\d.]+$/i);
          }, describe: function(e2, t2) {
            var r2 = { name: "DuckDuckGo" };
            if (t2) {
              var i2 = t2.getBrandVersion("DuckDuckGo");
              if (i2) return r2.version = i2, r2;
            }
            var a2 = n.default.getFirstMatch(/\sDdg\/([\d.]+)$/i, e2);
            return a2 && (r2.version = a2), r2;
          } }, { test: function(e2) {
            return e2.hasBrand("Brave");
          }, describe: function(e2, t2) {
            var r2 = { name: "Brave" };
            if (t2) {
              var i2 = t2.getBrandVersion("Brave");
              if (i2) return r2.version = i2, r2;
            }
            return r2;
          } }, { test: [/chromium/i], describe: function(e2) {
            var t2 = { name: "Chromium" }, r2 = n.default.getFirstMatch(/(?:chromium)[\s/](\d+(\.?_?\d+)+)/i, e2) || n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/chrome|crios|crmo/i], describe: function(e2) {
            var t2 = { name: "Chrome" }, r2 = n.default.getFirstMatch(/(?:chrome|crios|crmo)\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/GSA/i], describe: function(e2) {
            var t2 = { name: "Google Search" }, r2 = n.default.getFirstMatch(/(?:GSA)\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: function(e2) {
            var t2 = !e2.test(/like android/i), r2 = e2.test(/android/i);
            return t2 && r2;
          }, describe: function(e2) {
            var t2 = { name: "Android Browser" }, r2 = n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/playstation 4/i], describe: function(e2) {
            var t2 = { name: "PlayStation 4" }, r2 = n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/safari|applewebkit/i], describe: function(e2) {
            var t2 = { name: "Safari" }, r2 = n.default.getFirstMatch(a, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/.*/i], describe: function(e2) {
            var t2 = -1 !== e2.search("\\(") ? /^(.*)\/(.*)[ \t]\((.*)/ : /^(.*)\/(.*) /;
            return { name: n.default.getFirstMatch(t2, e2), version: n.default.getSecondMatch(t2, e2) };
          } }];
          t.default = o, e.exports = t.default;
        }, 93: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i, n = (i = r(17)) && i.__esModule ? i : { default: i }, a = r(18);
          var o = [{ test: [/Roku\/DVP/], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/Roku\/DVP-(\d+\.\d+)/i, e2);
            return { name: a.OS_MAP.Roku, version: t2 };
          } }, { test: [/windows phone/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/windows phone (?:os)?\s?(\d+(\.\d+)*)/i, e2);
            return { name: a.OS_MAP.WindowsPhone, version: t2 };
          } }, { test: [/windows /i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/Windows ((NT|XP)( \d\d?.\d)?)/i, e2), r2 = n.default.getWindowsVersionName(t2);
            return { name: a.OS_MAP.Windows, version: t2, versionName: r2 };
          } }, { test: [/Macintosh(.*?) FxiOS(.*?)\//], describe: function(e2) {
            var t2 = { name: a.OS_MAP.iOS }, r2 = n.default.getSecondMatch(/(Version\/)(\d[\d.]+)/, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/macintosh/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/mac os x (\d+(\.?_?\d+)+)/i, e2).replace(/[_\s]/g, "."), r2 = n.default.getMacOSVersionName(t2), i2 = { name: a.OS_MAP.MacOS, version: t2 };
            return r2 && (i2.versionName = r2), i2;
          } }, { test: [/(ipod|iphone|ipad)/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/os (\d+([_\s]\d+)*) like mac os x/i, e2).replace(/[_\s]/g, ".");
            return { name: a.OS_MAP.iOS, version: t2 };
          } }, { test: [/OpenHarmony/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/OpenHarmony\s+(\d+(\.\d+)*)/i, e2);
            return { name: a.OS_MAP.HarmonyOS, version: t2 };
          } }, { test: function(e2) {
            var t2 = !e2.test(/like android/i), r2 = e2.test(/android/i);
            return t2 && r2;
          }, describe: function(e2) {
            var t2 = n.default.getFirstMatch(/android[\s/-](\d+(\.\d+)*)/i, e2), r2 = n.default.getAndroidVersionName(t2), i2 = { name: a.OS_MAP.Android, version: t2 };
            return r2 && (i2.versionName = r2), i2;
          } }, { test: [/(web|hpw)[o0]s/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/(?:web|hpw)[o0]s\/(\d+(\.\d+)*)/i, e2), r2 = { name: a.OS_MAP.WebOS };
            return t2 && t2.length && (r2.version = t2), r2;
          } }, { test: [/blackberry|\bbb\d+/i, /rim\stablet/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/rim\stablet\sos\s(\d+(\.\d+)*)/i, e2) || n.default.getFirstMatch(/blackberry\d+\/(\d+([_\s]\d+)*)/i, e2) || n.default.getFirstMatch(/\bbb(\d+)/i, e2);
            return { name: a.OS_MAP.BlackBerry, version: t2 };
          } }, { test: [/bada/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/bada\/(\d+(\.\d+)*)/i, e2);
            return { name: a.OS_MAP.Bada, version: t2 };
          } }, { test: [/tizen/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/tizen[/\s](\d+(\.\d+)*)/i, e2);
            return { name: a.OS_MAP.Tizen, version: t2 };
          } }, { test: [/linux/i], describe: function() {
            return { name: a.OS_MAP.Linux };
          } }, { test: [/CrOS/], describe: function() {
            return { name: a.OS_MAP.ChromeOS };
          } }, { test: [/PlayStation 4/], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/PlayStation 4[/\s](\d+(\.\d+)*)/i, e2);
            return { name: a.OS_MAP.PlayStation4, version: t2 };
          } }];
          t.default = o, e.exports = t.default;
        }, 94: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i, n = (i = r(17)) && i.__esModule ? i : { default: i }, a = r(18);
          var o = [{ test: [/googlebot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Google" };
          } }, { test: [/linespider/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Line" };
          } }, { test: [/amazonbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Amazon" };
          } }, { test: [/gptbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "OpenAI" };
          } }, { test: [/chatgpt-user/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "OpenAI" };
          } }, { test: [/oai-searchbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "OpenAI" };
          } }, { test: [/baiduspider/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Baidu" };
          } }, { test: [/bingbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Bing" };
          } }, { test: [/duckduckbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "DuckDuckGo" };
          } }, { test: [/claudebot/i, /claude-web/i, /claude-user/i, /claude-searchbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Anthropic" };
          } }, { test: [/omgilibot/i, /webzio-extended/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Webz.io" };
          } }, { test: [/diffbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Diffbot" };
          } }, { test: [/perplexitybot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Perplexity AI" };
          } }, { test: [/perplexity-user/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Perplexity AI" };
          } }, { test: [/youbot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "You.com" };
          } }, { test: [/ia_archiver/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Internet Archive" };
          } }, { test: [/meta-webindexer/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Meta" };
          } }, { test: [/meta-externalads/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Meta" };
          } }, { test: [/meta-externalagent/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Meta" };
          } }, { test: [/meta-externalfetcher/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Meta" };
          } }, { test: [/facebookexternalhit/i, /facebookcatalog/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Meta" };
          } }, { test: [/slackbot/i, /slack-imgProxy/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Slack" };
          } }, { test: [/yahoo/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Yahoo" };
          } }, { test: [/yandexbot/i, /yandexmobilebot/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Yandex" };
          } }, { test: [/pingdom/i], describe: function() {
            return { type: a.PLATFORMS_MAP.bot, vendor: "Pingdom" };
          } }, { test: [/huawei/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/(can-l01)/i, e2) && "Nova", r2 = { type: a.PLATFORMS_MAP.mobile, vendor: "Huawei" };
            return t2 && (r2.model = t2), r2;
          } }, { test: [/nexus\s*(?:7|8|9|10).*/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet, vendor: "Nexus" };
          } }, { test: [/ipad/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet, vendor: "Apple", model: "iPad" };
          } }, { test: [/Macintosh(.*?) FxiOS(.*?)\//], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet, vendor: "Apple", model: "iPad" };
          } }, { test: [/kftt build/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet, vendor: "Amazon", model: "Kindle Fire HD 7" };
          } }, { test: [/silk/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet, vendor: "Amazon" };
          } }, { test: [/tablet(?! pc)/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tablet };
          } }, { test: function(e2) {
            var t2 = e2.test(/ipod|iphone/i), r2 = e2.test(/like (ipod|iphone)/i);
            return t2 && !r2;
          }, describe: function(e2) {
            var t2 = n.default.getFirstMatch(/(ipod|iphone)/i, e2);
            return { type: a.PLATFORMS_MAP.mobile, vendor: "Apple", model: t2 };
          } }, { test: [/nexus\s*[0-6].*/i, /galaxy nexus/i], describe: function() {
            return { type: a.PLATFORMS_MAP.mobile, vendor: "Nexus" };
          } }, { test: [/Nokia/i], describe: function(e2) {
            var t2 = n.default.getFirstMatch(/Nokia\s+([0-9]+(\.[0-9]+)?)/i, e2), r2 = { type: a.PLATFORMS_MAP.mobile, vendor: "Nokia" };
            return t2 && (r2.model = t2), r2;
          } }, { test: [/[^-]mobi/i], describe: function() {
            return { type: a.PLATFORMS_MAP.mobile };
          } }, { test: function(e2) {
            return "blackberry" === e2.getBrowserName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.mobile, vendor: "BlackBerry" };
          } }, { test: function(e2) {
            return "bada" === e2.getBrowserName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.mobile };
          } }, { test: function(e2) {
            return "windows phone" === e2.getBrowserName();
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.mobile, vendor: "Microsoft" };
          } }, { test: function(e2) {
            var t2 = Number(String(e2.getOSVersion()).split(".")[0]);
            return "android" === e2.getOSName(true) && t2 >= 3;
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.tablet };
          } }, { test: function(e2) {
            return "android" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.mobile };
          } }, { test: [/smart-?tv|smarttv/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tv };
          } }, { test: [/netcast/i], describe: function() {
            return { type: a.PLATFORMS_MAP.tv };
          } }, { test: function(e2) {
            return "macos" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.desktop, vendor: "Apple" };
          } }, { test: function(e2) {
            return "windows" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.desktop };
          } }, { test: function(e2) {
            return "linux" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.desktop };
          } }, { test: function(e2) {
            return "playstation 4" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.tv };
          } }, { test: function(e2) {
            return "roku" === e2.getOSName(true);
          }, describe: function() {
            return { type: a.PLATFORMS_MAP.tv };
          } }];
          t.default = o, e.exports = t.default;
        }, 95: function(e, t, r) {
          "use strict";
          t.__esModule = true, t.default = void 0;
          var i, n = (i = r(17)) && i.__esModule ? i : { default: i }, a = r(18);
          var o = [{ test: function(e2) {
            return "microsoft edge" === e2.getBrowserName(true);
          }, describe: function(e2) {
            if (/\sedg\//i.test(e2)) return { name: a.ENGINE_MAP.Blink };
            var t2 = n.default.getFirstMatch(/edge\/(\d+(\.?_?\d+)+)/i, e2);
            return { name: a.ENGINE_MAP.EdgeHTML, version: t2 };
          } }, { test: [/trident/i], describe: function(e2) {
            var t2 = { name: a.ENGINE_MAP.Trident }, r2 = n.default.getFirstMatch(/trident\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: function(e2) {
            return e2.test(/presto/i);
          }, describe: function(e2) {
            var t2 = { name: a.ENGINE_MAP.Presto }, r2 = n.default.getFirstMatch(/presto\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: function(e2) {
            var t2 = e2.test(/gecko/i), r2 = e2.test(/like gecko/i);
            return t2 && !r2;
          }, describe: function(e2) {
            var t2 = { name: a.ENGINE_MAP.Gecko }, r2 = n.default.getFirstMatch(/gecko\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }, { test: [/(apple)?webkit\/537\.36/i], describe: function() {
            return { name: a.ENGINE_MAP.Blink };
          } }, { test: [/(apple)?webkit/i], describe: function(e2) {
            var t2 = { name: a.ENGINE_MAP.WebKit }, r2 = n.default.getFirstMatch(/webkit\/(\d+(\.?_?\d+)+)/i, e2);
            return r2 && (t2.version = r2), t2;
          } }];
          t.default = o, e.exports = t.default;
        } });
      }));
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/extends.js
  var require_extends = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/extends.js"(exports, module) {
      function _extends() {
        return module.exports = _extends = Object.assign ? Object.assign.bind() : function(n) {
          for (var e = 1; e < arguments.length; e++) {
            var t = arguments[e];
            for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]);
          }
          return n;
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _extends.apply(null, arguments);
      }
      module.exports = _extends, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/typeof.js
  var require_typeof = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/typeof.js"(exports, module) {
      function _typeof(o) {
        "@babel/helpers - typeof";
        return module.exports = _typeof = "function" == typeof Symbol && "symbol" == typeof Symbol.iterator ? function(o2) {
          return typeof o2;
        } : function(o2) {
          return o2 && "function" == typeof Symbol && o2.constructor === Symbol && o2 !== Symbol.prototype ? "symbol" : typeof o2;
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _typeof(o);
      }
      module.exports = _typeof, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/toPrimitive.js
  var require_toPrimitive = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/toPrimitive.js"(exports, module) {
      var _typeof = require_typeof()["default"];
      function toPrimitive(t, r) {
        if ("object" != _typeof(t) || !t) return t;
        var e = t[Symbol.toPrimitive];
        if (void 0 !== e) {
          var i = e.call(t, r || "default");
          if ("object" != _typeof(i)) return i;
          throw new TypeError("@@toPrimitive must return a primitive value.");
        }
        return ("string" === r ? String : Number)(t);
      }
      module.exports = toPrimitive, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/toPropertyKey.js
  var require_toPropertyKey = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/toPropertyKey.js"(exports, module) {
      var _typeof = require_typeof()["default"];
      var toPrimitive = require_toPrimitive();
      function toPropertyKey(t) {
        var i = toPrimitive(t, "string");
        return "symbol" == _typeof(i) ? i : i + "";
      }
      module.exports = toPropertyKey, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/createClass.js
  var require_createClass = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/createClass.js"(exports, module) {
      var toPropertyKey = require_toPropertyKey();
      function _defineProperties(e, r) {
        for (var t = 0; t < r.length; t++) {
          var o = r[t];
          o.enumerable = o.enumerable || false, o.configurable = true, "value" in o && (o.writable = true), Object.defineProperty(e, toPropertyKey(o.key), o);
        }
      }
      function _createClass(e, r, t) {
        return r && _defineProperties(e.prototype, r), t && _defineProperties(e, t), Object.defineProperty(e, "prototype", {
          writable: false
        }), e;
      }
      module.exports = _createClass, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/asyncToGenerator.js
  var require_asyncToGenerator = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/asyncToGenerator.js"(exports, module) {
      function asyncGeneratorStep(n, t, e, r, o, a, c) {
        try {
          var i = n[a](c), u = i.value;
        } catch (n2) {
          return void e(n2);
        }
        i.done ? t(u) : Promise.resolve(u).then(r, o);
      }
      function _asyncToGenerator(n) {
        return function() {
          var t = this, e = arguments;
          return new Promise(function(r, o) {
            var a = n.apply(t, e);
            function _next(n2) {
              asyncGeneratorStep(a, r, o, _next, _throw, "next", n2);
            }
            function _throw(n2) {
              asyncGeneratorStep(a, r, o, _next, _throw, "throw", n2);
            }
            _next(void 0);
          });
        };
      }
      module.exports = _asyncToGenerator, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/OverloadYield.js
  var require_OverloadYield = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/OverloadYield.js"(exports, module) {
      function _OverloadYield(e, d) {
        this.v = e, this.k = d;
      }
      module.exports = _OverloadYield, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorDefine.js
  var require_regeneratorDefine = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorDefine.js"(exports, module) {
      function _regeneratorDefine(e, r, n, t) {
        var i = Object.defineProperty;
        try {
          i({}, "", {});
        } catch (e2) {
          i = 0;
        }
        module.exports = _regeneratorDefine = function regeneratorDefine(e2, r2, n2, t2) {
          function o(r3, n3) {
            _regeneratorDefine(e2, r3, function(e3) {
              return this._invoke(r3, n3, e3);
            });
          }
          r2 ? i ? i(e2, r2, {
            value: n2,
            enumerable: !t2,
            configurable: !t2,
            writable: !t2
          }) : e2[r2] = n2 : (o("next", 0), o("throw", 1), o("return", 2));
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _regeneratorDefine(e, r, n, t);
      }
      module.exports = _regeneratorDefine, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regenerator.js
  var require_regenerator = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regenerator.js"(exports, module) {
      var regeneratorDefine = require_regeneratorDefine();
      function _regenerator() {
        var e, t, r = "function" == typeof Symbol ? Symbol : {}, n = r.iterator || "@@iterator", o = r.toStringTag || "@@toStringTag";
        function i(r2, n2, o2, i2) {
          var c2 = n2 && n2.prototype instanceof Generator ? n2 : Generator, u2 = Object.create(c2.prototype);
          return regeneratorDefine(u2, "_invoke", (function(r3, n3, o3) {
            var i3, c3, u3, f2 = 0, p = o3 || [], y = false, G = {
              p: 0,
              n: 0,
              v: e,
              a: d,
              f: d.bind(e, 4),
              d: function d2(t2, r4) {
                return i3 = t2, c3 = 0, u3 = e, G.n = r4, a;
              }
            };
            function d(r4, n4) {
              for (c3 = r4, u3 = n4, t = 0; !y && f2 && !o4 && t < p.length; t++) {
                var o4, i4 = p[t], d2 = G.p, l = i4[2];
                r4 > 3 ? (o4 = l === n4) && (u3 = i4[(c3 = i4[4]) ? 5 : (c3 = 3, 3)], i4[4] = i4[5] = e) : i4[0] <= d2 && ((o4 = r4 < 2 && d2 < i4[1]) ? (c3 = 0, G.v = n4, G.n = i4[1]) : d2 < l && (o4 = r4 < 3 || i4[0] > n4 || n4 > l) && (i4[4] = r4, i4[5] = n4, G.n = l, c3 = 0));
              }
              if (o4 || r4 > 1) return a;
              throw y = true, n4;
            }
            return function(o4, p2, l) {
              if (f2 > 1) throw TypeError("Generator is already running");
              for (y && 1 === p2 && d(p2, l), c3 = p2, u3 = l; (t = c3 < 2 ? e : u3) || !y; ) {
                i3 || (c3 ? c3 < 3 ? (c3 > 1 && (G.n = -1), d(c3, u3)) : G.n = u3 : G.v = u3);
                try {
                  if (f2 = 2, i3) {
                    if (c3 || (o4 = "next"), t = i3[o4]) {
                      if (!(t = t.call(i3, u3))) throw TypeError("iterator result is not an object");
                      if (!t.done) return t;
                      u3 = t.value, c3 < 2 && (c3 = 0);
                    } else 1 === c3 && (t = i3["return"]) && t.call(i3), c3 < 2 && (u3 = TypeError("The iterator does not provide a '" + o4 + "' method"), c3 = 1);
                    i3 = e;
                  } else if ((t = (y = G.n < 0) ? u3 : r3.call(n3, G)) !== a) break;
                } catch (t2) {
                  i3 = e, c3 = 1, u3 = t2;
                } finally {
                  f2 = 1;
                }
              }
              return {
                value: t,
                done: y
              };
            };
          })(r2, o2, i2), true), u2;
        }
        var a = {};
        function Generator() {
        }
        function GeneratorFunction() {
        }
        function GeneratorFunctionPrototype() {
        }
        t = Object.getPrototypeOf;
        var c = [][n] ? t(t([][n]())) : (regeneratorDefine(t = {}, n, function() {
          return this;
        }), t), u = GeneratorFunctionPrototype.prototype = Generator.prototype = Object.create(c);
        function f(e2) {
          return Object.setPrototypeOf ? Object.setPrototypeOf(e2, GeneratorFunctionPrototype) : (e2.__proto__ = GeneratorFunctionPrototype, regeneratorDefine(e2, o, "GeneratorFunction")), e2.prototype = Object.create(u), e2;
        }
        return GeneratorFunction.prototype = GeneratorFunctionPrototype, regeneratorDefine(u, "constructor", GeneratorFunctionPrototype), regeneratorDefine(GeneratorFunctionPrototype, "constructor", GeneratorFunction), GeneratorFunction.displayName = "GeneratorFunction", regeneratorDefine(GeneratorFunctionPrototype, o, "GeneratorFunction"), regeneratorDefine(u), regeneratorDefine(u, o, "Generator"), regeneratorDefine(u, n, function() {
          return this;
        }), regeneratorDefine(u, "toString", function() {
          return "[object Generator]";
        }), (module.exports = _regenerator = function _regenerator2() {
          return {
            w: i,
            m: f
          };
        }, module.exports.__esModule = true, module.exports["default"] = module.exports)();
      }
      module.exports = _regenerator, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsyncIterator.js
  var require_regeneratorAsyncIterator = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsyncIterator.js"(exports, module) {
      var OverloadYield = require_OverloadYield();
      var regeneratorDefine = require_regeneratorDefine();
      function AsyncIterator(t, e) {
        function n(r2, o, i, f) {
          try {
            var c = t[r2](o), u = c.value;
            return u instanceof OverloadYield ? e.resolve(u.v).then(function(t2) {
              n("next", t2, i, f);
            }, function(t2) {
              n("throw", t2, i, f);
            }) : e.resolve(u).then(function(t2) {
              c.value = t2, i(c);
            }, function(t2) {
              return n("throw", t2, i, f);
            });
          } catch (t2) {
            f(t2);
          }
        }
        var r;
        this.next || (regeneratorDefine(AsyncIterator.prototype), regeneratorDefine(AsyncIterator.prototype, "function" == typeof Symbol && Symbol.asyncIterator || "@asyncIterator", function() {
          return this;
        })), regeneratorDefine(this, "_invoke", function(t2, o, i) {
          function f() {
            return new e(function(e2, r2) {
              n(t2, i, e2, r2);
            });
          }
          return r = r ? r.then(f, f) : f();
        }, true);
      }
      module.exports = AsyncIterator, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsyncGen.js
  var require_regeneratorAsyncGen = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsyncGen.js"(exports, module) {
      var regenerator = require_regenerator();
      var regeneratorAsyncIterator = require_regeneratorAsyncIterator();
      function _regeneratorAsyncGen(r, e, t, o, n) {
        return new regeneratorAsyncIterator(regenerator().w(r, e, t, o), n || Promise);
      }
      module.exports = _regeneratorAsyncGen, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsync.js
  var require_regeneratorAsync = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorAsync.js"(exports, module) {
      var regeneratorAsyncGen = require_regeneratorAsyncGen();
      function _regeneratorAsync(n, e, r, t, o) {
        var a = regeneratorAsyncGen(n, e, r, t, o);
        return a.next().then(function(n2) {
          return n2.done ? n2.value : a.next();
        });
      }
      module.exports = _regeneratorAsync, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorKeys.js
  var require_regeneratorKeys = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorKeys.js"(exports, module) {
      function _regeneratorKeys(e) {
        var n = Object(e), r = [];
        for (var t in n) r.unshift(t);
        return function e2() {
          for (; r.length; ) if ((t = r.pop()) in n) return e2.value = t, e2.done = false, e2;
          return e2.done = true, e2;
        };
      }
      module.exports = _regeneratorKeys, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorValues.js
  var require_regeneratorValues = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorValues.js"(exports, module) {
      var _typeof = require_typeof()["default"];
      function _regeneratorValues(e) {
        if (null != e) {
          var t = e["function" == typeof Symbol && Symbol.iterator || "@@iterator"], r = 0;
          if (t) return t.call(e);
          if ("function" == typeof e.next) return e;
          if (!isNaN(e.length)) return {
            next: function next() {
              return e && r >= e.length && (e = void 0), {
                value: e && e[r++],
                done: !e
              };
            }
          };
        }
        throw new TypeError(_typeof(e) + " is not iterable");
      }
      module.exports = _regeneratorValues, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorRuntime.js
  var require_regeneratorRuntime = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/regeneratorRuntime.js"(exports, module) {
      var OverloadYield = require_OverloadYield();
      var regenerator = require_regenerator();
      var regeneratorAsync = require_regeneratorAsync();
      var regeneratorAsyncGen = require_regeneratorAsyncGen();
      var regeneratorAsyncIterator = require_regeneratorAsyncIterator();
      var regeneratorKeys = require_regeneratorKeys();
      var regeneratorValues = require_regeneratorValues();
      function _regeneratorRuntime() {
        "use strict";
        var r = regenerator(), e = r.m(_regeneratorRuntime), t = (Object.getPrototypeOf ? Object.getPrototypeOf(e) : e.__proto__).constructor;
        function n(r2) {
          var e2 = "function" == typeof r2 && r2.constructor;
          return !!e2 && (e2 === t || "GeneratorFunction" === (e2.displayName || e2.name));
        }
        var o = {
          "throw": 1,
          "return": 2,
          "break": 3,
          "continue": 3
        };
        function a(r2) {
          var e2, t2;
          return function(n2) {
            e2 || (e2 = {
              stop: function stop() {
                return t2(n2.a, 2);
              },
              "catch": function _catch() {
                return n2.v;
              },
              abrupt: function abrupt(r3, e3) {
                return t2(n2.a, o[r3], e3);
              },
              delegateYield: function delegateYield(r3, o2, a2) {
                return e2.resultName = o2, t2(n2.d, regeneratorValues(r3), a2);
              },
              finish: function finish(r3) {
                return t2(n2.f, r3);
              }
            }, t2 = function t3(r3, _t, o2) {
              n2.p = e2.prev, n2.n = e2.next;
              try {
                return r3(_t, o2);
              } finally {
                e2.next = n2.n;
              }
            }), e2.resultName && (e2[e2.resultName] = n2.v, e2.resultName = void 0), e2.sent = n2.v, e2.next = n2.n;
            try {
              return r2.call(this, e2);
            } finally {
              n2.p = e2.prev, n2.n = e2.next;
            }
          };
        }
        return (module.exports = _regeneratorRuntime = function _regeneratorRuntime2() {
          return {
            wrap: function wrap(e2, t2, n2, o2) {
              return r.w(a(e2), t2, n2, o2 && o2.reverse());
            },
            isGeneratorFunction: n,
            mark: r.m,
            awrap: function awrap(r2, e2) {
              return new OverloadYield(r2, e2);
            },
            AsyncIterator: regeneratorAsyncIterator,
            async: function async(r2, e2, t2, o2, u) {
              return (n(e2) ? regeneratorAsyncGen : regeneratorAsync)(a(r2), e2, t2, o2, u);
            },
            keys: regeneratorKeys,
            values: regeneratorValues
          };
        }, module.exports.__esModule = true, module.exports["default"] = module.exports)();
      }
      module.exports = _regeneratorRuntime, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/regenerator/index.js
  var require_regenerator2 = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/regenerator/index.js"(exports, module) {
      var runtime = require_regeneratorRuntime()();
      module.exports = runtime;
      try {
        regeneratorRuntime = runtime;
      } catch (accidentalStrictMode) {
        if (typeof globalThis === "object") {
          globalThis.regeneratorRuntime = runtime;
        } else {
          Function("r", "regeneratorRuntime = r")(runtime);
        }
      }
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_listCacheClear.js
  var require_listCacheClear = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_listCacheClear.js"(exports, module) {
      function listCacheClear() {
        this.__data__ = [];
        this.size = 0;
      }
      module.exports = listCacheClear;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/eq.js
  var require_eq = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/eq.js"(exports, module) {
      function eq(value, other) {
        return value === other || value !== value && other !== other;
      }
      module.exports = eq;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_assocIndexOf.js
  var require_assocIndexOf = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_assocIndexOf.js"(exports, module) {
      var eq = require_eq();
      function assocIndexOf(array, key) {
        var length = array.length;
        while (length--) {
          if (eq(array[length][0], key)) {
            return length;
          }
        }
        return -1;
      }
      module.exports = assocIndexOf;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_listCacheDelete.js
  var require_listCacheDelete = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_listCacheDelete.js"(exports, module) {
      var assocIndexOf = require_assocIndexOf();
      var arrayProto = Array.prototype;
      var splice = arrayProto.splice;
      function listCacheDelete(key) {
        var data = this.__data__, index = assocIndexOf(data, key);
        if (index < 0) {
          return false;
        }
        var lastIndex = data.length - 1;
        if (index == lastIndex) {
          data.pop();
        } else {
          splice.call(data, index, 1);
        }
        --this.size;
        return true;
      }
      module.exports = listCacheDelete;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_listCacheGet.js
  var require_listCacheGet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_listCacheGet.js"(exports, module) {
      var assocIndexOf = require_assocIndexOf();
      function listCacheGet(key) {
        var data = this.__data__, index = assocIndexOf(data, key);
        return index < 0 ? void 0 : data[index][1];
      }
      module.exports = listCacheGet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_listCacheHas.js
  var require_listCacheHas = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_listCacheHas.js"(exports, module) {
      var assocIndexOf = require_assocIndexOf();
      function listCacheHas(key) {
        return assocIndexOf(this.__data__, key) > -1;
      }
      module.exports = listCacheHas;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_listCacheSet.js
  var require_listCacheSet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_listCacheSet.js"(exports, module) {
      var assocIndexOf = require_assocIndexOf();
      function listCacheSet(key, value) {
        var data = this.__data__, index = assocIndexOf(data, key);
        if (index < 0) {
          ++this.size;
          data.push([key, value]);
        } else {
          data[index][1] = value;
        }
        return this;
      }
      module.exports = listCacheSet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_ListCache.js
  var require_ListCache = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_ListCache.js"(exports, module) {
      var listCacheClear = require_listCacheClear();
      var listCacheDelete = require_listCacheDelete();
      var listCacheGet = require_listCacheGet();
      var listCacheHas = require_listCacheHas();
      var listCacheSet = require_listCacheSet();
      function ListCache(entries) {
        var index = -1, length = entries == null ? 0 : entries.length;
        this.clear();
        while (++index < length) {
          var entry = entries[index];
          this.set(entry[0], entry[1]);
        }
      }
      ListCache.prototype.clear = listCacheClear;
      ListCache.prototype["delete"] = listCacheDelete;
      ListCache.prototype.get = listCacheGet;
      ListCache.prototype.has = listCacheHas;
      ListCache.prototype.set = listCacheSet;
      module.exports = ListCache;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_stackClear.js
  var require_stackClear = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_stackClear.js"(exports, module) {
      var ListCache = require_ListCache();
      function stackClear() {
        this.__data__ = new ListCache();
        this.size = 0;
      }
      module.exports = stackClear;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_stackDelete.js
  var require_stackDelete = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_stackDelete.js"(exports, module) {
      function stackDelete(key) {
        var data = this.__data__, result = data["delete"](key);
        this.size = data.size;
        return result;
      }
      module.exports = stackDelete;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_stackGet.js
  var require_stackGet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_stackGet.js"(exports, module) {
      function stackGet(key) {
        return this.__data__.get(key);
      }
      module.exports = stackGet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_stackHas.js
  var require_stackHas = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_stackHas.js"(exports, module) {
      function stackHas(key) {
        return this.__data__.has(key);
      }
      module.exports = stackHas;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_freeGlobal.js
  var require_freeGlobal = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_freeGlobal.js"(exports, module) {
      var freeGlobal = typeof global == "object" && global && global.Object === Object && global;
      module.exports = freeGlobal;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_root.js
  var require_root = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_root.js"(exports, module) {
      var freeGlobal = require_freeGlobal();
      var freeSelf = typeof self == "object" && self && self.Object === Object && self;
      var root = freeGlobal || freeSelf || Function("return this")();
      module.exports = root;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_Symbol.js
  var require_Symbol = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_Symbol.js"(exports, module) {
      var root = require_root();
      var Symbol2 = root.Symbol;
      module.exports = Symbol2;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_getRawTag.js
  var require_getRawTag = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_getRawTag.js"(exports, module) {
      var Symbol2 = require_Symbol();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      var nativeObjectToString = objectProto.toString;
      var symToStringTag = Symbol2 ? Symbol2.toStringTag : void 0;
      function getRawTag(value) {
        var isOwn = hasOwnProperty.call(value, symToStringTag), tag = value[symToStringTag];
        try {
          value[symToStringTag] = void 0;
          var unmasked = true;
        } catch (e) {
        }
        var result = nativeObjectToString.call(value);
        if (unmasked) {
          if (isOwn) {
            value[symToStringTag] = tag;
          } else {
            delete value[symToStringTag];
          }
        }
        return result;
      }
      module.exports = getRawTag;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_objectToString.js
  var require_objectToString = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_objectToString.js"(exports, module) {
      var objectProto = Object.prototype;
      var nativeObjectToString = objectProto.toString;
      function objectToString(value) {
        return nativeObjectToString.call(value);
      }
      module.exports = objectToString;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseGetTag.js
  var require_baseGetTag = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseGetTag.js"(exports, module) {
      var Symbol2 = require_Symbol();
      var getRawTag = require_getRawTag();
      var objectToString = require_objectToString();
      var nullTag = "[object Null]";
      var undefinedTag = "[object Undefined]";
      var symToStringTag = Symbol2 ? Symbol2.toStringTag : void 0;
      function baseGetTag(value) {
        if (value == null) {
          return value === void 0 ? undefinedTag : nullTag;
        }
        return symToStringTag && symToStringTag in Object(value) ? getRawTag(value) : objectToString(value);
      }
      module.exports = baseGetTag;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isObject.js
  var require_isObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isObject.js"(exports, module) {
      function isObject(value) {
        var type = typeof value;
        return value != null && (type == "object" || type == "function");
      }
      module.exports = isObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isFunction.js
  var require_isFunction = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isFunction.js"(exports, module) {
      var baseGetTag = require_baseGetTag();
      var isObject = require_isObject();
      var asyncTag = "[object AsyncFunction]";
      var funcTag = "[object Function]";
      var genTag = "[object GeneratorFunction]";
      var proxyTag = "[object Proxy]";
      function isFunction(value) {
        if (!isObject(value)) {
          return false;
        }
        var tag = baseGetTag(value);
        return tag == funcTag || tag == genTag || tag == asyncTag || tag == proxyTag;
      }
      module.exports = isFunction;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_coreJsData.js
  var require_coreJsData = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_coreJsData.js"(exports, module) {
      var root = require_root();
      var coreJsData = root["__core-js_shared__"];
      module.exports = coreJsData;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_isMasked.js
  var require_isMasked = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_isMasked.js"(exports, module) {
      var coreJsData = require_coreJsData();
      var maskSrcKey = (function() {
        var uid = /[^.]+$/.exec(coreJsData && coreJsData.keys && coreJsData.keys.IE_PROTO || "");
        return uid ? "Symbol(src)_1." + uid : "";
      })();
      function isMasked(func) {
        return !!maskSrcKey && maskSrcKey in func;
      }
      module.exports = isMasked;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_toSource.js
  var require_toSource = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_toSource.js"(exports, module) {
      var funcProto = Function.prototype;
      var funcToString = funcProto.toString;
      function toSource(func) {
        if (func != null) {
          try {
            return funcToString.call(func);
          } catch (e) {
          }
          try {
            return func + "";
          } catch (e) {
          }
        }
        return "";
      }
      module.exports = toSource;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseIsNative.js
  var require_baseIsNative = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseIsNative.js"(exports, module) {
      var isFunction = require_isFunction();
      var isMasked = require_isMasked();
      var isObject = require_isObject();
      var toSource = require_toSource();
      var reRegExpChar = /[\\^$.*+?()[\]{}|]/g;
      var reIsHostCtor = /^\[object .+?Constructor\]$/;
      var funcProto = Function.prototype;
      var objectProto = Object.prototype;
      var funcToString = funcProto.toString;
      var hasOwnProperty = objectProto.hasOwnProperty;
      var reIsNative = RegExp(
        "^" + funcToString.call(hasOwnProperty).replace(reRegExpChar, "\\$&").replace(/hasOwnProperty|(function).*?(?=\\\()| for .+?(?=\\\])/g, "$1.*?") + "$"
      );
      function baseIsNative(value) {
        if (!isObject(value) || isMasked(value)) {
          return false;
        }
        var pattern = isFunction(value) ? reIsNative : reIsHostCtor;
        return pattern.test(toSource(value));
      }
      module.exports = baseIsNative;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_getValue.js
  var require_getValue = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_getValue.js"(exports, module) {
      function getValue(object, key) {
        return object == null ? void 0 : object[key];
      }
      module.exports = getValue;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_getNative.js
  var require_getNative = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_getNative.js"(exports, module) {
      var baseIsNative = require_baseIsNative();
      var getValue = require_getValue();
      function getNative(object, key) {
        var value = getValue(object, key);
        return baseIsNative(value) ? value : void 0;
      }
      module.exports = getNative;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_Map.js
  var require_Map = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_Map.js"(exports, module) {
      var getNative = require_getNative();
      var root = require_root();
      var Map2 = getNative(root, "Map");
      module.exports = Map2;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_nativeCreate.js
  var require_nativeCreate = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_nativeCreate.js"(exports, module) {
      var getNative = require_getNative();
      var nativeCreate = getNative(Object, "create");
      module.exports = nativeCreate;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_hashClear.js
  var require_hashClear = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_hashClear.js"(exports, module) {
      var nativeCreate = require_nativeCreate();
      function hashClear() {
        this.__data__ = nativeCreate ? nativeCreate(null) : {};
        this.size = 0;
      }
      module.exports = hashClear;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_hashDelete.js
  var require_hashDelete = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_hashDelete.js"(exports, module) {
      function hashDelete(key) {
        var result = this.has(key) && delete this.__data__[key];
        this.size -= result ? 1 : 0;
        return result;
      }
      module.exports = hashDelete;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_hashGet.js
  var require_hashGet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_hashGet.js"(exports, module) {
      var nativeCreate = require_nativeCreate();
      var HASH_UNDEFINED = "__lodash_hash_undefined__";
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      function hashGet(key) {
        var data = this.__data__;
        if (nativeCreate) {
          var result = data[key];
          return result === HASH_UNDEFINED ? void 0 : result;
        }
        return hasOwnProperty.call(data, key) ? data[key] : void 0;
      }
      module.exports = hashGet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_hashHas.js
  var require_hashHas = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_hashHas.js"(exports, module) {
      var nativeCreate = require_nativeCreate();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      function hashHas(key) {
        var data = this.__data__;
        return nativeCreate ? data[key] !== void 0 : hasOwnProperty.call(data, key);
      }
      module.exports = hashHas;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_hashSet.js
  var require_hashSet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_hashSet.js"(exports, module) {
      var nativeCreate = require_nativeCreate();
      var HASH_UNDEFINED = "__lodash_hash_undefined__";
      function hashSet(key, value) {
        var data = this.__data__;
        this.size += this.has(key) ? 0 : 1;
        data[key] = nativeCreate && value === void 0 ? HASH_UNDEFINED : value;
        return this;
      }
      module.exports = hashSet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_Hash.js
  var require_Hash = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_Hash.js"(exports, module) {
      var hashClear = require_hashClear();
      var hashDelete = require_hashDelete();
      var hashGet = require_hashGet();
      var hashHas = require_hashHas();
      var hashSet = require_hashSet();
      function Hash(entries) {
        var index = -1, length = entries == null ? 0 : entries.length;
        this.clear();
        while (++index < length) {
          var entry = entries[index];
          this.set(entry[0], entry[1]);
        }
      }
      Hash.prototype.clear = hashClear;
      Hash.prototype["delete"] = hashDelete;
      Hash.prototype.get = hashGet;
      Hash.prototype.has = hashHas;
      Hash.prototype.set = hashSet;
      module.exports = Hash;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_mapCacheClear.js
  var require_mapCacheClear = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_mapCacheClear.js"(exports, module) {
      var Hash = require_Hash();
      var ListCache = require_ListCache();
      var Map2 = require_Map();
      function mapCacheClear() {
        this.size = 0;
        this.__data__ = {
          "hash": new Hash(),
          "map": new (Map2 || ListCache)(),
          "string": new Hash()
        };
      }
      module.exports = mapCacheClear;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_isKeyable.js
  var require_isKeyable = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_isKeyable.js"(exports, module) {
      function isKeyable(value) {
        var type = typeof value;
        return type == "string" || type == "number" || type == "symbol" || type == "boolean" ? value !== "__proto__" : value === null;
      }
      module.exports = isKeyable;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_getMapData.js
  var require_getMapData = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_getMapData.js"(exports, module) {
      var isKeyable = require_isKeyable();
      function getMapData(map, key) {
        var data = map.__data__;
        return isKeyable(key) ? data[typeof key == "string" ? "string" : "hash"] : data.map;
      }
      module.exports = getMapData;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_mapCacheDelete.js
  var require_mapCacheDelete = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_mapCacheDelete.js"(exports, module) {
      var getMapData = require_getMapData();
      function mapCacheDelete(key) {
        var result = getMapData(this, key)["delete"](key);
        this.size -= result ? 1 : 0;
        return result;
      }
      module.exports = mapCacheDelete;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_mapCacheGet.js
  var require_mapCacheGet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_mapCacheGet.js"(exports, module) {
      var getMapData = require_getMapData();
      function mapCacheGet(key) {
        return getMapData(this, key).get(key);
      }
      module.exports = mapCacheGet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_mapCacheHas.js
  var require_mapCacheHas = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_mapCacheHas.js"(exports, module) {
      var getMapData = require_getMapData();
      function mapCacheHas(key) {
        return getMapData(this, key).has(key);
      }
      module.exports = mapCacheHas;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_mapCacheSet.js
  var require_mapCacheSet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_mapCacheSet.js"(exports, module) {
      var getMapData = require_getMapData();
      function mapCacheSet(key, value) {
        var data = getMapData(this, key), size = data.size;
        data.set(key, value);
        this.size += data.size == size ? 0 : 1;
        return this;
      }
      module.exports = mapCacheSet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_MapCache.js
  var require_MapCache = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_MapCache.js"(exports, module) {
      var mapCacheClear = require_mapCacheClear();
      var mapCacheDelete = require_mapCacheDelete();
      var mapCacheGet = require_mapCacheGet();
      var mapCacheHas = require_mapCacheHas();
      var mapCacheSet = require_mapCacheSet();
      function MapCache(entries) {
        var index = -1, length = entries == null ? 0 : entries.length;
        this.clear();
        while (++index < length) {
          var entry = entries[index];
          this.set(entry[0], entry[1]);
        }
      }
      MapCache.prototype.clear = mapCacheClear;
      MapCache.prototype["delete"] = mapCacheDelete;
      MapCache.prototype.get = mapCacheGet;
      MapCache.prototype.has = mapCacheHas;
      MapCache.prototype.set = mapCacheSet;
      module.exports = MapCache;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_stackSet.js
  var require_stackSet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_stackSet.js"(exports, module) {
      var ListCache = require_ListCache();
      var Map2 = require_Map();
      var MapCache = require_MapCache();
      var LARGE_ARRAY_SIZE = 200;
      function stackSet(key, value) {
        var data = this.__data__;
        if (data instanceof ListCache) {
          var pairs = data.__data__;
          if (!Map2 || pairs.length < LARGE_ARRAY_SIZE - 1) {
            pairs.push([key, value]);
            this.size = ++data.size;
            return this;
          }
          data = this.__data__ = new MapCache(pairs);
        }
        data.set(key, value);
        this.size = data.size;
        return this;
      }
      module.exports = stackSet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_Stack.js
  var require_Stack = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_Stack.js"(exports, module) {
      var ListCache = require_ListCache();
      var stackClear = require_stackClear();
      var stackDelete = require_stackDelete();
      var stackGet = require_stackGet();
      var stackHas = require_stackHas();
      var stackSet = require_stackSet();
      function Stack(entries) {
        var data = this.__data__ = new ListCache(entries);
        this.size = data.size;
      }
      Stack.prototype.clear = stackClear;
      Stack.prototype["delete"] = stackDelete;
      Stack.prototype.get = stackGet;
      Stack.prototype.has = stackHas;
      Stack.prototype.set = stackSet;
      module.exports = Stack;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_defineProperty.js
  var require_defineProperty = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_defineProperty.js"(exports, module) {
      var getNative = require_getNative();
      var defineProperty = (function() {
        try {
          var func = getNative(Object, "defineProperty");
          func({}, "", {});
          return func;
        } catch (e) {
        }
      })();
      module.exports = defineProperty;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseAssignValue.js
  var require_baseAssignValue = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseAssignValue.js"(exports, module) {
      var defineProperty = require_defineProperty();
      function baseAssignValue(object, key, value) {
        if (key == "__proto__" && defineProperty) {
          defineProperty(object, key, {
            "configurable": true,
            "enumerable": true,
            "value": value,
            "writable": true
          });
        } else {
          object[key] = value;
        }
      }
      module.exports = baseAssignValue;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_assignMergeValue.js
  var require_assignMergeValue = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_assignMergeValue.js"(exports, module) {
      var baseAssignValue = require_baseAssignValue();
      var eq = require_eq();
      function assignMergeValue(object, key, value) {
        if (value !== void 0 && !eq(object[key], value) || value === void 0 && !(key in object)) {
          baseAssignValue(object, key, value);
        }
      }
      module.exports = assignMergeValue;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_createBaseFor.js
  var require_createBaseFor = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_createBaseFor.js"(exports, module) {
      function createBaseFor(fromRight) {
        return function(object, iteratee, keysFunc) {
          var index = -1, iterable = Object(object), props = keysFunc(object), length = props.length;
          while (length--) {
            var key = props[fromRight ? length : ++index];
            if (iteratee(iterable[key], key, iterable) === false) {
              break;
            }
          }
          return object;
        };
      }
      module.exports = createBaseFor;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseFor.js
  var require_baseFor = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseFor.js"(exports, module) {
      var createBaseFor = require_createBaseFor();
      var baseFor = createBaseFor();
      module.exports = baseFor;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_cloneBuffer.js
  var require_cloneBuffer = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_cloneBuffer.js"(exports, module) {
      var root = require_root();
      var freeExports = typeof exports == "object" && exports && !exports.nodeType && exports;
      var freeModule = freeExports && typeof module == "object" && module && !module.nodeType && module;
      var moduleExports = freeModule && freeModule.exports === freeExports;
      var Buffer2 = moduleExports ? root.Buffer : void 0;
      var allocUnsafe = Buffer2 ? Buffer2.allocUnsafe : void 0;
      function cloneBuffer(buffer, isDeep) {
        if (isDeep) {
          return buffer.slice();
        }
        var length = buffer.length, result = allocUnsafe ? allocUnsafe(length) : new buffer.constructor(length);
        buffer.copy(result);
        return result;
      }
      module.exports = cloneBuffer;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_Uint8Array.js
  var require_Uint8Array = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_Uint8Array.js"(exports, module) {
      var root = require_root();
      var Uint8Array2 = root.Uint8Array;
      module.exports = Uint8Array2;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_cloneArrayBuffer.js
  var require_cloneArrayBuffer = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_cloneArrayBuffer.js"(exports, module) {
      var Uint8Array2 = require_Uint8Array();
      function cloneArrayBuffer(arrayBuffer) {
        var result = new arrayBuffer.constructor(arrayBuffer.byteLength);
        new Uint8Array2(result).set(new Uint8Array2(arrayBuffer));
        return result;
      }
      module.exports = cloneArrayBuffer;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_cloneTypedArray.js
  var require_cloneTypedArray = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_cloneTypedArray.js"(exports, module) {
      var cloneArrayBuffer = require_cloneArrayBuffer();
      function cloneTypedArray(typedArray, isDeep) {
        var buffer = isDeep ? cloneArrayBuffer(typedArray.buffer) : typedArray.buffer;
        return new typedArray.constructor(buffer, typedArray.byteOffset, typedArray.length);
      }
      module.exports = cloneTypedArray;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_copyArray.js
  var require_copyArray = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_copyArray.js"(exports, module) {
      function copyArray(source, array) {
        var index = -1, length = source.length;
        array || (array = Array(length));
        while (++index < length) {
          array[index] = source[index];
        }
        return array;
      }
      module.exports = copyArray;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseCreate.js
  var require_baseCreate = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseCreate.js"(exports, module) {
      var isObject = require_isObject();
      var objectCreate = Object.create;
      var baseCreate = /* @__PURE__ */ (function() {
        function object() {
        }
        return function(proto) {
          if (!isObject(proto)) {
            return {};
          }
          if (objectCreate) {
            return objectCreate(proto);
          }
          object.prototype = proto;
          var result = new object();
          object.prototype = void 0;
          return result;
        };
      })();
      module.exports = baseCreate;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_overArg.js
  var require_overArg = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_overArg.js"(exports, module) {
      function overArg(func, transform) {
        return function(arg) {
          return func(transform(arg));
        };
      }
      module.exports = overArg;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_getPrototype.js
  var require_getPrototype = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_getPrototype.js"(exports, module) {
      var overArg = require_overArg();
      var getPrototype = overArg(Object.getPrototypeOf, Object);
      module.exports = getPrototype;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_isPrototype.js
  var require_isPrototype = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_isPrototype.js"(exports, module) {
      var objectProto = Object.prototype;
      function isPrototype(value) {
        var Ctor = value && value.constructor, proto = typeof Ctor == "function" && Ctor.prototype || objectProto;
        return value === proto;
      }
      module.exports = isPrototype;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_initCloneObject.js
  var require_initCloneObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_initCloneObject.js"(exports, module) {
      var baseCreate = require_baseCreate();
      var getPrototype = require_getPrototype();
      var isPrototype = require_isPrototype();
      function initCloneObject(object) {
        return typeof object.constructor == "function" && !isPrototype(object) ? baseCreate(getPrototype(object)) : {};
      }
      module.exports = initCloneObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isObjectLike.js
  var require_isObjectLike = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isObjectLike.js"(exports, module) {
      function isObjectLike(value) {
        return value != null && typeof value == "object";
      }
      module.exports = isObjectLike;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseIsArguments.js
  var require_baseIsArguments = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseIsArguments.js"(exports, module) {
      var baseGetTag = require_baseGetTag();
      var isObjectLike = require_isObjectLike();
      var argsTag = "[object Arguments]";
      function baseIsArguments(value) {
        return isObjectLike(value) && baseGetTag(value) == argsTag;
      }
      module.exports = baseIsArguments;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isArguments.js
  var require_isArguments = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isArguments.js"(exports, module) {
      var baseIsArguments = require_baseIsArguments();
      var isObjectLike = require_isObjectLike();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      var propertyIsEnumerable = objectProto.propertyIsEnumerable;
      var isArguments = baseIsArguments(/* @__PURE__ */ (function() {
        return arguments;
      })()) ? baseIsArguments : function(value) {
        return isObjectLike(value) && hasOwnProperty.call(value, "callee") && !propertyIsEnumerable.call(value, "callee");
      };
      module.exports = isArguments;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isArray.js
  var require_isArray = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isArray.js"(exports, module) {
      var isArray = Array.isArray;
      module.exports = isArray;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isLength.js
  var require_isLength = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isLength.js"(exports, module) {
      var MAX_SAFE_INTEGER = 9007199254740991;
      function isLength(value) {
        return typeof value == "number" && value > -1 && value % 1 == 0 && value <= MAX_SAFE_INTEGER;
      }
      module.exports = isLength;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isArrayLike.js
  var require_isArrayLike = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isArrayLike.js"(exports, module) {
      var isFunction = require_isFunction();
      var isLength = require_isLength();
      function isArrayLike(value) {
        return value != null && isLength(value.length) && !isFunction(value);
      }
      module.exports = isArrayLike;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isArrayLikeObject.js
  var require_isArrayLikeObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isArrayLikeObject.js"(exports, module) {
      var isArrayLike = require_isArrayLike();
      var isObjectLike = require_isObjectLike();
      function isArrayLikeObject(value) {
        return isObjectLike(value) && isArrayLike(value);
      }
      module.exports = isArrayLikeObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/stubFalse.js
  var require_stubFalse = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/stubFalse.js"(exports, module) {
      function stubFalse() {
        return false;
      }
      module.exports = stubFalse;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isBuffer.js
  var require_isBuffer = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isBuffer.js"(exports, module) {
      var root = require_root();
      var stubFalse = require_stubFalse();
      var freeExports = typeof exports == "object" && exports && !exports.nodeType && exports;
      var freeModule = freeExports && typeof module == "object" && module && !module.nodeType && module;
      var moduleExports = freeModule && freeModule.exports === freeExports;
      var Buffer2 = moduleExports ? root.Buffer : void 0;
      var nativeIsBuffer = Buffer2 ? Buffer2.isBuffer : void 0;
      var isBuffer = nativeIsBuffer || stubFalse;
      module.exports = isBuffer;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isPlainObject.js
  var require_isPlainObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isPlainObject.js"(exports, module) {
      var baseGetTag = require_baseGetTag();
      var getPrototype = require_getPrototype();
      var isObjectLike = require_isObjectLike();
      var objectTag = "[object Object]";
      var funcProto = Function.prototype;
      var objectProto = Object.prototype;
      var funcToString = funcProto.toString;
      var hasOwnProperty = objectProto.hasOwnProperty;
      var objectCtorString = funcToString.call(Object);
      function isPlainObject(value) {
        if (!isObjectLike(value) || baseGetTag(value) != objectTag) {
          return false;
        }
        var proto = getPrototype(value);
        if (proto === null) {
          return true;
        }
        var Ctor = hasOwnProperty.call(proto, "constructor") && proto.constructor;
        return typeof Ctor == "function" && Ctor instanceof Ctor && funcToString.call(Ctor) == objectCtorString;
      }
      module.exports = isPlainObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseIsTypedArray.js
  var require_baseIsTypedArray = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseIsTypedArray.js"(exports, module) {
      var baseGetTag = require_baseGetTag();
      var isLength = require_isLength();
      var isObjectLike = require_isObjectLike();
      var argsTag = "[object Arguments]";
      var arrayTag = "[object Array]";
      var boolTag = "[object Boolean]";
      var dateTag = "[object Date]";
      var errorTag = "[object Error]";
      var funcTag = "[object Function]";
      var mapTag = "[object Map]";
      var numberTag = "[object Number]";
      var objectTag = "[object Object]";
      var regexpTag = "[object RegExp]";
      var setTag = "[object Set]";
      var stringTag = "[object String]";
      var weakMapTag = "[object WeakMap]";
      var arrayBufferTag = "[object ArrayBuffer]";
      var dataViewTag = "[object DataView]";
      var float32Tag = "[object Float32Array]";
      var float64Tag = "[object Float64Array]";
      var int8Tag = "[object Int8Array]";
      var int16Tag = "[object Int16Array]";
      var int32Tag = "[object Int32Array]";
      var uint8Tag = "[object Uint8Array]";
      var uint8ClampedTag = "[object Uint8ClampedArray]";
      var uint16Tag = "[object Uint16Array]";
      var uint32Tag = "[object Uint32Array]";
      var typedArrayTags = {};
      typedArrayTags[float32Tag] = typedArrayTags[float64Tag] = typedArrayTags[int8Tag] = typedArrayTags[int16Tag] = typedArrayTags[int32Tag] = typedArrayTags[uint8Tag] = typedArrayTags[uint8ClampedTag] = typedArrayTags[uint16Tag] = typedArrayTags[uint32Tag] = true;
      typedArrayTags[argsTag] = typedArrayTags[arrayTag] = typedArrayTags[arrayBufferTag] = typedArrayTags[boolTag] = typedArrayTags[dataViewTag] = typedArrayTags[dateTag] = typedArrayTags[errorTag] = typedArrayTags[funcTag] = typedArrayTags[mapTag] = typedArrayTags[numberTag] = typedArrayTags[objectTag] = typedArrayTags[regexpTag] = typedArrayTags[setTag] = typedArrayTags[stringTag] = typedArrayTags[weakMapTag] = false;
      function baseIsTypedArray(value) {
        return isObjectLike(value) && isLength(value.length) && !!typedArrayTags[baseGetTag(value)];
      }
      module.exports = baseIsTypedArray;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseUnary.js
  var require_baseUnary = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseUnary.js"(exports, module) {
      function baseUnary(func) {
        return function(value) {
          return func(value);
        };
      }
      module.exports = baseUnary;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_nodeUtil.js
  var require_nodeUtil = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_nodeUtil.js"(exports, module) {
      var freeGlobal = require_freeGlobal();
      var freeExports = typeof exports == "object" && exports && !exports.nodeType && exports;
      var freeModule = freeExports && typeof module == "object" && module && !module.nodeType && module;
      var moduleExports = freeModule && freeModule.exports === freeExports;
      var freeProcess = moduleExports && freeGlobal.process;
      var nodeUtil = (function() {
        try {
          var types = freeModule && freeModule.require && freeModule.require("util").types;
          if (types) {
            return types;
          }
          return freeProcess && freeProcess.binding && freeProcess.binding("util");
        } catch (e) {
        }
      })();
      module.exports = nodeUtil;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/isTypedArray.js
  var require_isTypedArray = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/isTypedArray.js"(exports, module) {
      var baseIsTypedArray = require_baseIsTypedArray();
      var baseUnary = require_baseUnary();
      var nodeUtil = require_nodeUtil();
      var nodeIsTypedArray = nodeUtil && nodeUtil.isTypedArray;
      var isTypedArray = nodeIsTypedArray ? baseUnary(nodeIsTypedArray) : baseIsTypedArray;
      module.exports = isTypedArray;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_safeGet.js
  var require_safeGet = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_safeGet.js"(exports, module) {
      function safeGet(object, key) {
        if (key === "constructor" && typeof object[key] === "function") {
          return;
        }
        if (key == "__proto__") {
          return;
        }
        return object[key];
      }
      module.exports = safeGet;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_assignValue.js
  var require_assignValue = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_assignValue.js"(exports, module) {
      var baseAssignValue = require_baseAssignValue();
      var eq = require_eq();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      function assignValue(object, key, value) {
        var objValue = object[key];
        if (!(hasOwnProperty.call(object, key) && eq(objValue, value)) || value === void 0 && !(key in object)) {
          baseAssignValue(object, key, value);
        }
      }
      module.exports = assignValue;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_copyObject.js
  var require_copyObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_copyObject.js"(exports, module) {
      var assignValue = require_assignValue();
      var baseAssignValue = require_baseAssignValue();
      function copyObject(source, props, object, customizer) {
        var isNew = !object;
        object || (object = {});
        var index = -1, length = props.length;
        while (++index < length) {
          var key = props[index];
          var newValue = customizer ? customizer(object[key], source[key], key, object, source) : void 0;
          if (newValue === void 0) {
            newValue = source[key];
          }
          if (isNew) {
            baseAssignValue(object, key, newValue);
          } else {
            assignValue(object, key, newValue);
          }
        }
        return object;
      }
      module.exports = copyObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseTimes.js
  var require_baseTimes = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseTimes.js"(exports, module) {
      function baseTimes(n, iteratee) {
        var index = -1, result = Array(n);
        while (++index < n) {
          result[index] = iteratee(index);
        }
        return result;
      }
      module.exports = baseTimes;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_isIndex.js
  var require_isIndex = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_isIndex.js"(exports, module) {
      var MAX_SAFE_INTEGER = 9007199254740991;
      var reIsUint = /^(?:0|[1-9]\d*)$/;
      function isIndex(value, length) {
        var type = typeof value;
        length = length == null ? MAX_SAFE_INTEGER : length;
        return !!length && (type == "number" || type != "symbol" && reIsUint.test(value)) && (value > -1 && value % 1 == 0 && value < length);
      }
      module.exports = isIndex;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_arrayLikeKeys.js
  var require_arrayLikeKeys = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_arrayLikeKeys.js"(exports, module) {
      var baseTimes = require_baseTimes();
      var isArguments = require_isArguments();
      var isArray = require_isArray();
      var isBuffer = require_isBuffer();
      var isIndex = require_isIndex();
      var isTypedArray = require_isTypedArray();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      function arrayLikeKeys(value, inherited) {
        var isArr = isArray(value), isArg = !isArr && isArguments(value), isBuff = !isArr && !isArg && isBuffer(value), isType = !isArr && !isArg && !isBuff && isTypedArray(value), skipIndexes = isArr || isArg || isBuff || isType, result = skipIndexes ? baseTimes(value.length, String) : [], length = result.length;
        for (var key in value) {
          if ((inherited || hasOwnProperty.call(value, key)) && !(skipIndexes && // Safari 9 has enumerable `arguments.length` in strict mode.
          (key == "length" || // Node.js 0.10 has enumerable non-index properties on buffers.
          isBuff && (key == "offset" || key == "parent") || // PhantomJS 2 has enumerable non-index properties on typed arrays.
          isType && (key == "buffer" || key == "byteLength" || key == "byteOffset") || // Skip index properties.
          isIndex(key, length)))) {
            result.push(key);
          }
        }
        return result;
      }
      module.exports = arrayLikeKeys;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_nativeKeysIn.js
  var require_nativeKeysIn = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_nativeKeysIn.js"(exports, module) {
      function nativeKeysIn(object) {
        var result = [];
        if (object != null) {
          for (var key in Object(object)) {
            result.push(key);
          }
        }
        return result;
      }
      module.exports = nativeKeysIn;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseKeysIn.js
  var require_baseKeysIn = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseKeysIn.js"(exports, module) {
      var isObject = require_isObject();
      var isPrototype = require_isPrototype();
      var nativeKeysIn = require_nativeKeysIn();
      var objectProto = Object.prototype;
      var hasOwnProperty = objectProto.hasOwnProperty;
      function baseKeysIn(object) {
        if (!isObject(object)) {
          return nativeKeysIn(object);
        }
        var isProto = isPrototype(object), result = [];
        for (var key in object) {
          if (!(key == "constructor" && (isProto || !hasOwnProperty.call(object, key)))) {
            result.push(key);
          }
        }
        return result;
      }
      module.exports = baseKeysIn;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/keysIn.js
  var require_keysIn = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/keysIn.js"(exports, module) {
      var arrayLikeKeys = require_arrayLikeKeys();
      var baseKeysIn = require_baseKeysIn();
      var isArrayLike = require_isArrayLike();
      function keysIn(object) {
        return isArrayLike(object) ? arrayLikeKeys(object, true) : baseKeysIn(object);
      }
      module.exports = keysIn;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/toPlainObject.js
  var require_toPlainObject = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/toPlainObject.js"(exports, module) {
      var copyObject = require_copyObject();
      var keysIn = require_keysIn();
      function toPlainObject(value) {
        return copyObject(value, keysIn(value));
      }
      module.exports = toPlainObject;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseMergeDeep.js
  var require_baseMergeDeep = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseMergeDeep.js"(exports, module) {
      var assignMergeValue = require_assignMergeValue();
      var cloneBuffer = require_cloneBuffer();
      var cloneTypedArray = require_cloneTypedArray();
      var copyArray = require_copyArray();
      var initCloneObject = require_initCloneObject();
      var isArguments = require_isArguments();
      var isArray = require_isArray();
      var isArrayLikeObject = require_isArrayLikeObject();
      var isBuffer = require_isBuffer();
      var isFunction = require_isFunction();
      var isObject = require_isObject();
      var isPlainObject = require_isPlainObject();
      var isTypedArray = require_isTypedArray();
      var safeGet = require_safeGet();
      var toPlainObject = require_toPlainObject();
      function baseMergeDeep(object, source, key, srcIndex, mergeFunc, customizer, stack) {
        var objValue = safeGet(object, key), srcValue = safeGet(source, key), stacked = stack.get(srcValue);
        if (stacked) {
          assignMergeValue(object, key, stacked);
          return;
        }
        var newValue = customizer ? customizer(objValue, srcValue, key + "", object, source, stack) : void 0;
        var isCommon = newValue === void 0;
        if (isCommon) {
          var isArr = isArray(srcValue), isBuff = !isArr && isBuffer(srcValue), isTyped = !isArr && !isBuff && isTypedArray(srcValue);
          newValue = srcValue;
          if (isArr || isBuff || isTyped) {
            if (isArray(objValue)) {
              newValue = objValue;
            } else if (isArrayLikeObject(objValue)) {
              newValue = copyArray(objValue);
            } else if (isBuff) {
              isCommon = false;
              newValue = cloneBuffer(srcValue, true);
            } else if (isTyped) {
              isCommon = false;
              newValue = cloneTypedArray(srcValue, true);
            } else {
              newValue = [];
            }
          } else if (isPlainObject(srcValue) || isArguments(srcValue)) {
            newValue = objValue;
            if (isArguments(objValue)) {
              newValue = toPlainObject(objValue);
            } else if (!isObject(objValue) || isFunction(objValue)) {
              newValue = initCloneObject(srcValue);
            }
          } else {
            isCommon = false;
          }
        }
        if (isCommon) {
          stack.set(srcValue, newValue);
          mergeFunc(newValue, srcValue, srcIndex, customizer, stack);
          stack["delete"](srcValue);
        }
        assignMergeValue(object, key, newValue);
      }
      module.exports = baseMergeDeep;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseMerge.js
  var require_baseMerge = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseMerge.js"(exports, module) {
      var Stack = require_Stack();
      var assignMergeValue = require_assignMergeValue();
      var baseFor = require_baseFor();
      var baseMergeDeep = require_baseMergeDeep();
      var isObject = require_isObject();
      var keysIn = require_keysIn();
      var safeGet = require_safeGet();
      function baseMerge(object, source, srcIndex, customizer, stack) {
        if (object === source) {
          return;
        }
        baseFor(source, function(srcValue, key) {
          stack || (stack = new Stack());
          if (isObject(srcValue)) {
            baseMergeDeep(object, source, key, srcIndex, baseMerge, customizer, stack);
          } else {
            var newValue = customizer ? customizer(safeGet(object, key), srcValue, key + "", object, source, stack) : void 0;
            if (newValue === void 0) {
              newValue = srcValue;
            }
            assignMergeValue(object, key, newValue);
          }
        }, keysIn);
      }
      module.exports = baseMerge;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/identity.js
  var require_identity = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/identity.js"(exports, module) {
      function identity(value) {
        return value;
      }
      module.exports = identity;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_apply.js
  var require_apply = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_apply.js"(exports, module) {
      function apply(func, thisArg, args) {
        switch (args.length) {
          case 0:
            return func.call(thisArg);
          case 1:
            return func.call(thisArg, args[0]);
          case 2:
            return func.call(thisArg, args[0], args[1]);
          case 3:
            return func.call(thisArg, args[0], args[1], args[2]);
        }
        return func.apply(thisArg, args);
      }
      module.exports = apply;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_overRest.js
  var require_overRest = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_overRest.js"(exports, module) {
      var apply = require_apply();
      var nativeMax = Math.max;
      function overRest(func, start, transform) {
        start = nativeMax(start === void 0 ? func.length - 1 : start, 0);
        return function() {
          var args = arguments, index = -1, length = nativeMax(args.length - start, 0), array = Array(length);
          while (++index < length) {
            array[index] = args[start + index];
          }
          index = -1;
          var otherArgs = Array(start + 1);
          while (++index < start) {
            otherArgs[index] = args[index];
          }
          otherArgs[start] = transform(array);
          return apply(func, this, otherArgs);
        };
      }
      module.exports = overRest;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/constant.js
  var require_constant = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/constant.js"(exports, module) {
      function constant(value) {
        return function() {
          return value;
        };
      }
      module.exports = constant;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseSetToString.js
  var require_baseSetToString = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseSetToString.js"(exports, module) {
      var constant = require_constant();
      var defineProperty = require_defineProperty();
      var identity = require_identity();
      var baseSetToString = !defineProperty ? identity : function(func, string) {
        return defineProperty(func, "toString", {
          "configurable": true,
          "enumerable": false,
          "value": constant(string),
          "writable": true
        });
      };
      module.exports = baseSetToString;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_shortOut.js
  var require_shortOut = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_shortOut.js"(exports, module) {
      var HOT_COUNT = 800;
      var HOT_SPAN = 16;
      var nativeNow = Date.now;
      function shortOut(func) {
        var count = 0, lastCalled = 0;
        return function() {
          var stamp = nativeNow(), remaining = HOT_SPAN - (stamp - lastCalled);
          lastCalled = stamp;
          if (remaining > 0) {
            if (++count >= HOT_COUNT) {
              return arguments[0];
            }
          } else {
            count = 0;
          }
          return func.apply(void 0, arguments);
        };
      }
      module.exports = shortOut;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_setToString.js
  var require_setToString = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_setToString.js"(exports, module) {
      var baseSetToString = require_baseSetToString();
      var shortOut = require_shortOut();
      var setToString = shortOut(baseSetToString);
      module.exports = setToString;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_baseRest.js
  var require_baseRest = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_baseRest.js"(exports, module) {
      var identity = require_identity();
      var overRest = require_overRest();
      var setToString = require_setToString();
      function baseRest(func, start) {
        return setToString(overRest(func, start, identity), func + "");
      }
      module.exports = baseRest;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_isIterateeCall.js
  var require_isIterateeCall = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_isIterateeCall.js"(exports, module) {
      var eq = require_eq();
      var isArrayLike = require_isArrayLike();
      var isIndex = require_isIndex();
      var isObject = require_isObject();
      function isIterateeCall(value, index, object) {
        if (!isObject(object)) {
          return false;
        }
        var type = typeof index;
        if (type == "number" ? isArrayLike(object) && isIndex(index, object.length) : type == "string" && index in object) {
          return eq(object[index], value);
        }
        return false;
      }
      module.exports = isIterateeCall;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/_createAssigner.js
  var require_createAssigner = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/_createAssigner.js"(exports, module) {
      var baseRest = require_baseRest();
      var isIterateeCall = require_isIterateeCall();
      function createAssigner(assigner) {
        return baseRest(function(object, sources) {
          var index = -1, length = sources.length, customizer = length > 1 ? sources[length - 1] : void 0, guard = length > 2 ? sources[2] : void 0;
          customizer = assigner.length > 3 && typeof customizer == "function" ? (length--, customizer) : void 0;
          if (guard && isIterateeCall(sources[0], sources[1], guard)) {
            customizer = length < 3 ? void 0 : customizer;
            length = 1;
          }
          object = Object(object);
          while (++index < length) {
            var source = sources[index];
            if (source) {
              assigner(object, source, index, customizer);
            }
          }
          return object;
        });
      }
      module.exports = createAssigner;
    }
  });

  // /tmp/ivs-build/node_modules/lodash/mergeWith.js
  var require_mergeWith = __commonJS({
    "/tmp/ivs-build/node_modules/lodash/mergeWith.js"(exports, module) {
      var baseMerge = require_baseMerge();
      var createAssigner = require_createAssigner();
      var mergeWith = createAssigner(function(object, source, srcIndex, customizer) {
        baseMerge(object, source, srcIndex, customizer);
      });
      module.exports = mergeWith;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/readOnlyError.js
  var require_readOnlyError = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/readOnlyError.js"(exports, module) {
      function _readOnlyError(r) {
        throw new TypeError('"' + r + '" is read-only');
      }
      module.exports = _readOnlyError, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/events/events.js
  var require_events = __commonJS({
    "/tmp/ivs-build/node_modules/events/events.js"(exports, module) {
      "use strict";
      var R = typeof Reflect === "object" ? Reflect : null;
      var ReflectApply = R && typeof R.apply === "function" ? R.apply : function ReflectApply2(target, receiver, args) {
        return Function.prototype.apply.call(target, receiver, args);
      };
      var ReflectOwnKeys;
      if (R && typeof R.ownKeys === "function") {
        ReflectOwnKeys = R.ownKeys;
      } else if (Object.getOwnPropertySymbols) {
        ReflectOwnKeys = function ReflectOwnKeys2(target) {
          return Object.getOwnPropertyNames(target).concat(Object.getOwnPropertySymbols(target));
        };
      } else {
        ReflectOwnKeys = function ReflectOwnKeys2(target) {
          return Object.getOwnPropertyNames(target);
        };
      }
      function ProcessEmitWarning(warning) {
        if (console && console.warn) console.warn(warning);
      }
      var NumberIsNaN = Number.isNaN || function NumberIsNaN2(value) {
        return value !== value;
      };
      function EventEmitter() {
        EventEmitter.init.call(this);
      }
      module.exports = EventEmitter;
      module.exports.once = once;
      EventEmitter.EventEmitter = EventEmitter;
      EventEmitter.prototype._events = void 0;
      EventEmitter.prototype._eventsCount = 0;
      EventEmitter.prototype._maxListeners = void 0;
      var defaultMaxListeners = 10;
      function checkListener(listener) {
        if (typeof listener !== "function") {
          throw new TypeError('The "listener" argument must be of type Function. Received type ' + typeof listener);
        }
      }
      Object.defineProperty(EventEmitter, "defaultMaxListeners", {
        enumerable: true,
        get: function() {
          return defaultMaxListeners;
        },
        set: function(arg) {
          if (typeof arg !== "number" || arg < 0 || NumberIsNaN(arg)) {
            throw new RangeError('The value of "defaultMaxListeners" is out of range. It must be a non-negative number. Received ' + arg + ".");
          }
          defaultMaxListeners = arg;
        }
      });
      EventEmitter.init = function() {
        if (this._events === void 0 || this._events === Object.getPrototypeOf(this)._events) {
          this._events = /* @__PURE__ */ Object.create(null);
          this._eventsCount = 0;
        }
        this._maxListeners = this._maxListeners || void 0;
      };
      EventEmitter.prototype.setMaxListeners = function setMaxListeners(n) {
        if (typeof n !== "number" || n < 0 || NumberIsNaN(n)) {
          throw new RangeError('The value of "n" is out of range. It must be a non-negative number. Received ' + n + ".");
        }
        this._maxListeners = n;
        return this;
      };
      function _getMaxListeners(that) {
        if (that._maxListeners === void 0)
          return EventEmitter.defaultMaxListeners;
        return that._maxListeners;
      }
      EventEmitter.prototype.getMaxListeners = function getMaxListeners() {
        return _getMaxListeners(this);
      };
      EventEmitter.prototype.emit = function emit(type) {
        var args = [];
        for (var i = 1; i < arguments.length; i++) args.push(arguments[i]);
        var doError = type === "error";
        var events = this._events;
        if (events !== void 0)
          doError = doError && events.error === void 0;
        else if (!doError)
          return false;
        if (doError) {
          var er;
          if (args.length > 0)
            er = args[0];
          if (er instanceof Error) {
            throw er;
          }
          var err = new Error("Unhandled error." + (er ? " (" + er.message + ")" : ""));
          err.context = er;
          throw err;
        }
        var handler = events[type];
        if (handler === void 0)
          return false;
        if (typeof handler === "function") {
          ReflectApply(handler, this, args);
        } else {
          var len = handler.length;
          var listeners = arrayClone(handler, len);
          for (var i = 0; i < len; ++i)
            ReflectApply(listeners[i], this, args);
        }
        return true;
      };
      function _addListener(target, type, listener, prepend) {
        var m;
        var events;
        var existing;
        checkListener(listener);
        events = target._events;
        if (events === void 0) {
          events = target._events = /* @__PURE__ */ Object.create(null);
          target._eventsCount = 0;
        } else {
          if (events.newListener !== void 0) {
            target.emit(
              "newListener",
              type,
              listener.listener ? listener.listener : listener
            );
            events = target._events;
          }
          existing = events[type];
        }
        if (existing === void 0) {
          existing = events[type] = listener;
          ++target._eventsCount;
        } else {
          if (typeof existing === "function") {
            existing = events[type] = prepend ? [listener, existing] : [existing, listener];
          } else if (prepend) {
            existing.unshift(listener);
          } else {
            existing.push(listener);
          }
          m = _getMaxListeners(target);
          if (m > 0 && existing.length > m && !existing.warned) {
            existing.warned = true;
            var w = new Error("Possible EventEmitter memory leak detected. " + existing.length + " " + String(type) + " listeners added. Use emitter.setMaxListeners() to increase limit");
            w.name = "MaxListenersExceededWarning";
            w.emitter = target;
            w.type = type;
            w.count = existing.length;
            ProcessEmitWarning(w);
          }
        }
        return target;
      }
      EventEmitter.prototype.addListener = function addListener(type, listener) {
        return _addListener(this, type, listener, false);
      };
      EventEmitter.prototype.on = EventEmitter.prototype.addListener;
      EventEmitter.prototype.prependListener = function prependListener(type, listener) {
        return _addListener(this, type, listener, true);
      };
      function onceWrapper() {
        if (!this.fired) {
          this.target.removeListener(this.type, this.wrapFn);
          this.fired = true;
          if (arguments.length === 0)
            return this.listener.call(this.target);
          return this.listener.apply(this.target, arguments);
        }
      }
      function _onceWrap(target, type, listener) {
        var state = { fired: false, wrapFn: void 0, target, type, listener };
        var wrapped = onceWrapper.bind(state);
        wrapped.listener = listener;
        state.wrapFn = wrapped;
        return wrapped;
      }
      EventEmitter.prototype.once = function once2(type, listener) {
        checkListener(listener);
        this.on(type, _onceWrap(this, type, listener));
        return this;
      };
      EventEmitter.prototype.prependOnceListener = function prependOnceListener(type, listener) {
        checkListener(listener);
        this.prependListener(type, _onceWrap(this, type, listener));
        return this;
      };
      EventEmitter.prototype.removeListener = function removeListener(type, listener) {
        var list, events, position, i, originalListener;
        checkListener(listener);
        events = this._events;
        if (events === void 0)
          return this;
        list = events[type];
        if (list === void 0)
          return this;
        if (list === listener || list.listener === listener) {
          if (--this._eventsCount === 0)
            this._events = /* @__PURE__ */ Object.create(null);
          else {
            delete events[type];
            if (events.removeListener)
              this.emit("removeListener", type, list.listener || listener);
          }
        } else if (typeof list !== "function") {
          position = -1;
          for (i = list.length - 1; i >= 0; i--) {
            if (list[i] === listener || list[i].listener === listener) {
              originalListener = list[i].listener;
              position = i;
              break;
            }
          }
          if (position < 0)
            return this;
          if (position === 0)
            list.shift();
          else {
            spliceOne(list, position);
          }
          if (list.length === 1)
            events[type] = list[0];
          if (events.removeListener !== void 0)
            this.emit("removeListener", type, originalListener || listener);
        }
        return this;
      };
      EventEmitter.prototype.off = EventEmitter.prototype.removeListener;
      EventEmitter.prototype.removeAllListeners = function removeAllListeners(type) {
        var listeners, events, i;
        events = this._events;
        if (events === void 0)
          return this;
        if (events.removeListener === void 0) {
          if (arguments.length === 0) {
            this._events = /* @__PURE__ */ Object.create(null);
            this._eventsCount = 0;
          } else if (events[type] !== void 0) {
            if (--this._eventsCount === 0)
              this._events = /* @__PURE__ */ Object.create(null);
            else
              delete events[type];
          }
          return this;
        }
        if (arguments.length === 0) {
          var keys = Object.keys(events);
          var key;
          for (i = 0; i < keys.length; ++i) {
            key = keys[i];
            if (key === "removeListener") continue;
            this.removeAllListeners(key);
          }
          this.removeAllListeners("removeListener");
          this._events = /* @__PURE__ */ Object.create(null);
          this._eventsCount = 0;
          return this;
        }
        listeners = events[type];
        if (typeof listeners === "function") {
          this.removeListener(type, listeners);
        } else if (listeners !== void 0) {
          for (i = listeners.length - 1; i >= 0; i--) {
            this.removeListener(type, listeners[i]);
          }
        }
        return this;
      };
      function _listeners(target, type, unwrap) {
        var events = target._events;
        if (events === void 0)
          return [];
        var evlistener = events[type];
        if (evlistener === void 0)
          return [];
        if (typeof evlistener === "function")
          return unwrap ? [evlistener.listener || evlistener] : [evlistener];
        return unwrap ? unwrapListeners(evlistener) : arrayClone(evlistener, evlistener.length);
      }
      EventEmitter.prototype.listeners = function listeners(type) {
        return _listeners(this, type, true);
      };
      EventEmitter.prototype.rawListeners = function rawListeners(type) {
        return _listeners(this, type, false);
      };
      EventEmitter.listenerCount = function(emitter, type) {
        if (typeof emitter.listenerCount === "function") {
          return emitter.listenerCount(type);
        } else {
          return listenerCount.call(emitter, type);
        }
      };
      EventEmitter.prototype.listenerCount = listenerCount;
      function listenerCount(type) {
        var events = this._events;
        if (events !== void 0) {
          var evlistener = events[type];
          if (typeof evlistener === "function") {
            return 1;
          } else if (evlistener !== void 0) {
            return evlistener.length;
          }
        }
        return 0;
      }
      EventEmitter.prototype.eventNames = function eventNames() {
        return this._eventsCount > 0 ? ReflectOwnKeys(this._events) : [];
      };
      function arrayClone(arr, n) {
        var copy = new Array(n);
        for (var i = 0; i < n; ++i)
          copy[i] = arr[i];
        return copy;
      }
      function spliceOne(list, index) {
        for (; index + 1 < list.length; index++)
          list[index] = list[index + 1];
        list.pop();
      }
      function unwrapListeners(arr) {
        var ret = new Array(arr.length);
        for (var i = 0; i < ret.length; ++i) {
          ret[i] = arr[i].listener || arr[i];
        }
        return ret;
      }
      function once(emitter, name) {
        return new Promise(function(resolve, reject) {
          function errorListener(err) {
            emitter.removeListener(name, resolver);
            reject(err);
          }
          function resolver() {
            if (typeof emitter.removeListener === "function") {
              emitter.removeListener("error", errorListener);
            }
            resolve([].slice.call(arguments));
          }
          ;
          eventTargetAgnosticAddListener(emitter, name, resolver, { once: true });
          if (name !== "error") {
            addErrorHandlerIfEventEmitter(emitter, errorListener, { once: true });
          }
        });
      }
      function addErrorHandlerIfEventEmitter(emitter, handler, flags) {
        if (typeof emitter.on === "function") {
          eventTargetAgnosticAddListener(emitter, "error", handler, flags);
        }
      }
      function eventTargetAgnosticAddListener(emitter, name, listener, flags) {
        if (typeof emitter.on === "function") {
          if (flags.once) {
            emitter.once(name, listener);
          } else {
            emitter.on(name, listener);
          }
        } else if (typeof emitter.addEventListener === "function") {
          emitter.addEventListener(name, function wrapListener(arg) {
            if (flags.once) {
              emitter.removeEventListener(name, wrapListener);
            }
            listener(arg);
          });
        } else {
          throw new TypeError('The "emitter" argument must be of type EventEmitter. Received type ' + typeof emitter);
        }
      }
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/setPrototypeOf.js
  var require_setPrototypeOf = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/setPrototypeOf.js"(exports, module) {
      function _setPrototypeOf(t, e) {
        return module.exports = _setPrototypeOf = Object.setPrototypeOf ? Object.setPrototypeOf.bind() : function(t2, e2) {
          return t2.__proto__ = e2, t2;
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _setPrototypeOf(t, e);
      }
      module.exports = _setPrototypeOf, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/inheritsLoose.js
  var require_inheritsLoose = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/inheritsLoose.js"(exports, module) {
      var setPrototypeOf = require_setPrototypeOf();
      function _inheritsLoose(t, o) {
        t.prototype = Object.create(o.prototype), t.prototype.constructor = t, setPrototypeOf(t, o);
      }
      module.exports = _inheritsLoose, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/promise-polyfill/lib/index.js
  var require_lib = __commonJS({
    "/tmp/ivs-build/node_modules/promise-polyfill/lib/index.js"(exports, module) {
      "use strict";
      function finallyConstructor(callback) {
        var constructor = this.constructor;
        return this.then(
          function(value) {
            return constructor.resolve(callback()).then(function() {
              return value;
            });
          },
          function(reason) {
            return constructor.resolve(callback()).then(function() {
              return constructor.reject(reason);
            });
          }
        );
      }
      function allSettled(arr) {
        var P = this;
        return new P(function(resolve2, reject2) {
          if (!(arr && typeof arr.length !== "undefined")) {
            return reject2(
              new TypeError(
                typeof arr + " " + arr + " is not iterable(cannot read property Symbol(Symbol.iterator))"
              )
            );
          }
          var args = Array.prototype.slice.call(arr);
          if (args.length === 0) return resolve2([]);
          var remaining = args.length;
          function res(i2, val) {
            if (val && (typeof val === "object" || typeof val === "function")) {
              var then = val.then;
              if (typeof then === "function") {
                then.call(
                  val,
                  function(val2) {
                    res(i2, val2);
                  },
                  function(e) {
                    args[i2] = { status: "rejected", reason: e };
                    if (--remaining === 0) {
                      resolve2(args);
                    }
                  }
                );
                return;
              }
            }
            args[i2] = { status: "fulfilled", value: val };
            if (--remaining === 0) {
              resolve2(args);
            }
          }
          for (var i = 0; i < args.length; i++) {
            res(i, args[i]);
          }
        });
      }
      function AggregateError(errors, message) {
        this.name = "AggregateError", this.errors = errors;
        this.message = message || "";
      }
      AggregateError.prototype = Error.prototype;
      function any(arr) {
        var P = this;
        return new P(function(resolve2, reject2) {
          if (!(arr && typeof arr.length !== "undefined")) {
            return reject2(new TypeError("Promise.any accepts an array"));
          }
          var args = Array.prototype.slice.call(arr);
          if (args.length === 0) return reject2();
          var rejectionReasons = [];
          for (var i = 0; i < args.length; i++) {
            try {
              P.resolve(args[i]).then(resolve2).catch(function(error) {
                rejectionReasons.push(error);
                if (rejectionReasons.length === args.length) {
                  reject2(
                    new AggregateError(
                      rejectionReasons,
                      "All promises were rejected"
                    )
                  );
                }
              });
            } catch (ex) {
              reject2(ex);
            }
          }
        });
      }
      var setTimeoutFunc = setTimeout;
      function isArray(x) {
        return Boolean(x && typeof x.length !== "undefined");
      }
      function noop() {
      }
      function bind(fn, thisArg) {
        return function() {
          fn.apply(thisArg, arguments);
        };
      }
      function Promise2(fn) {
        if (!(this instanceof Promise2))
          throw new TypeError("Promises must be constructed via new");
        if (typeof fn !== "function") throw new TypeError("not a function");
        this._state = 0;
        this._handled = false;
        this._value = void 0;
        this._deferreds = [];
        doResolve(fn, this);
      }
      function handle(self2, deferred) {
        while (self2._state === 3) {
          self2 = self2._value;
        }
        if (self2._state === 0) {
          self2._deferreds.push(deferred);
          return;
        }
        self2._handled = true;
        Promise2._immediateFn(function() {
          var cb = self2._state === 1 ? deferred.onFulfilled : deferred.onRejected;
          if (cb === null) {
            (self2._state === 1 ? resolve : reject)(deferred.promise, self2._value);
            return;
          }
          var ret;
          try {
            ret = cb(self2._value);
          } catch (e) {
            reject(deferred.promise, e);
            return;
          }
          resolve(deferred.promise, ret);
        });
      }
      function resolve(self2, newValue) {
        try {
          if (newValue === self2)
            throw new TypeError("A promise cannot be resolved with itself.");
          if (newValue && (typeof newValue === "object" || typeof newValue === "function")) {
            var then = newValue.then;
            if (newValue instanceof Promise2) {
              self2._state = 3;
              self2._value = newValue;
              finale(self2);
              return;
            } else if (typeof then === "function") {
              doResolve(bind(then, newValue), self2);
              return;
            }
          }
          self2._state = 1;
          self2._value = newValue;
          finale(self2);
        } catch (e) {
          reject(self2, e);
        }
      }
      function reject(self2, newValue) {
        self2._state = 2;
        self2._value = newValue;
        finale(self2);
      }
      function finale(self2) {
        if (self2._state === 2 && self2._deferreds.length === 0) {
          Promise2._immediateFn(function() {
            if (!self2._handled) {
              Promise2._unhandledRejectionFn(self2._value);
            }
          });
        }
        for (var i = 0, len = self2._deferreds.length; i < len; i++) {
          handle(self2, self2._deferreds[i]);
        }
        self2._deferreds = null;
      }
      function Handler(onFulfilled, onRejected, promise) {
        this.onFulfilled = typeof onFulfilled === "function" ? onFulfilled : null;
        this.onRejected = typeof onRejected === "function" ? onRejected : null;
        this.promise = promise;
      }
      function doResolve(fn, self2) {
        var done = false;
        try {
          fn(
            function(value) {
              if (done) return;
              done = true;
              resolve(self2, value);
            },
            function(reason) {
              if (done) return;
              done = true;
              reject(self2, reason);
            }
          );
        } catch (ex) {
          if (done) return;
          done = true;
          reject(self2, ex);
        }
      }
      Promise2.prototype["catch"] = function(onRejected) {
        return this.then(null, onRejected);
      };
      Promise2.prototype.then = function(onFulfilled, onRejected) {
        var prom = new this.constructor(noop);
        handle(this, new Handler(onFulfilled, onRejected, prom));
        return prom;
      };
      Promise2.prototype["finally"] = finallyConstructor;
      Promise2.all = function(arr) {
        return new Promise2(function(resolve2, reject2) {
          if (!isArray(arr)) {
            return reject2(new TypeError("Promise.all accepts an array"));
          }
          var args = Array.prototype.slice.call(arr);
          if (args.length === 0) return resolve2([]);
          var remaining = args.length;
          function res(i2, val) {
            try {
              if (val && (typeof val === "object" || typeof val === "function")) {
                var then = val.then;
                if (typeof then === "function") {
                  then.call(
                    val,
                    function(val2) {
                      res(i2, val2);
                    },
                    reject2
                  );
                  return;
                }
              }
              args[i2] = val;
              if (--remaining === 0) {
                resolve2(args);
              }
            } catch (ex) {
              reject2(ex);
            }
          }
          for (var i = 0; i < args.length; i++) {
            res(i, args[i]);
          }
        });
      };
      Promise2.any = any;
      Promise2.allSettled = allSettled;
      Promise2.resolve = function(value) {
        if (value && typeof value === "object" && value.constructor === Promise2) {
          return value;
        }
        return new Promise2(function(resolve2) {
          resolve2(value);
        });
      };
      Promise2.reject = function(value) {
        return new Promise2(function(resolve2, reject2) {
          reject2(value);
        });
      };
      Promise2.race = function(arr) {
        return new Promise2(function(resolve2, reject2) {
          if (!isArray(arr)) {
            return reject2(new TypeError("Promise.race accepts an array"));
          }
          for (var i = 0, len = arr.length; i < len; i++) {
            Promise2.resolve(arr[i]).then(resolve2, reject2);
          }
        });
      };
      Promise2._immediateFn = // @ts-ignore
      typeof setImmediate === "function" && function(fn) {
        setImmediate(fn);
      } || function(fn) {
        setTimeoutFunc(fn, 0);
      };
      Promise2._unhandledRejectionFn = function _unhandledRejectionFn(err) {
        if (typeof console !== "undefined" && console) {
          console.warn("Possible Unhandled Promise Rejection:", err);
        }
      };
      module.exports = Promise2;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/getPrototypeOf.js
  var require_getPrototypeOf = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/getPrototypeOf.js"(exports, module) {
      function _getPrototypeOf(t) {
        return module.exports = _getPrototypeOf = Object.setPrototypeOf ? Object.getPrototypeOf.bind() : function(t2) {
          return t2.__proto__ || Object.getPrototypeOf(t2);
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _getPrototypeOf(t);
      }
      module.exports = _getPrototypeOf, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/isNativeFunction.js
  var require_isNativeFunction = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/isNativeFunction.js"(exports, module) {
      function _isNativeFunction(t) {
        try {
          return -1 !== Function.toString.call(t).indexOf("[native code]");
        } catch (n) {
          return "function" == typeof t;
        }
      }
      module.exports = _isNativeFunction, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/isNativeReflectConstruct.js
  var require_isNativeReflectConstruct = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/isNativeReflectConstruct.js"(exports, module) {
      function _isNativeReflectConstruct() {
        try {
          var t = !Boolean.prototype.valueOf.call(Reflect.construct(Boolean, [], function() {
          }));
        } catch (t2) {
        }
        return (module.exports = _isNativeReflectConstruct = function _isNativeReflectConstruct2() {
          return !!t;
        }, module.exports.__esModule = true, module.exports["default"] = module.exports)();
      }
      module.exports = _isNativeReflectConstruct, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/construct.js
  var require_construct = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/construct.js"(exports, module) {
      var isNativeReflectConstruct = require_isNativeReflectConstruct();
      var setPrototypeOf = require_setPrototypeOf();
      function _construct(t, e, r) {
        if (isNativeReflectConstruct()) return Reflect.construct.apply(null, arguments);
        var o = [null];
        o.push.apply(o, e);
        var p = new (t.bind.apply(t, o))();
        return r && setPrototypeOf(p, r.prototype), p;
      }
      module.exports = _construct, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/@babel/runtime/helpers/wrapNativeSuper.js
  var require_wrapNativeSuper = __commonJS({
    "/tmp/ivs-build/node_modules/@babel/runtime/helpers/wrapNativeSuper.js"(exports, module) {
      var getPrototypeOf = require_getPrototypeOf();
      var setPrototypeOf = require_setPrototypeOf();
      var isNativeFunction = require_isNativeFunction();
      var construct = require_construct();
      function _wrapNativeSuper(t) {
        var r = "function" == typeof Map ? /* @__PURE__ */ new Map() : void 0;
        return module.exports = _wrapNativeSuper = function _wrapNativeSuper2(t2) {
          if (null === t2 || !isNativeFunction(t2)) return t2;
          if ("function" != typeof t2) throw new TypeError("Super expression must either be null or a function");
          if (void 0 !== r) {
            if (r.has(t2)) return r.get(t2);
            r.set(t2, Wrapper);
          }
          function Wrapper() {
            return construct(t2, arguments, getPrototypeOf(this).constructor);
          }
          return Wrapper.prototype = Object.create(t2.prototype, {
            constructor: {
              value: Wrapper,
              enumerable: false,
              writable: true,
              configurable: true
            }
          }), setPrototypeOf(Wrapper, t2);
        }, module.exports.__esModule = true, module.exports["default"] = module.exports, _wrapNativeSuper(t);
      }
      module.exports = _wrapNativeSuper, module.exports.__esModule = true, module.exports["default"] = module.exports;
    }
  });

  // /tmp/ivs-build/node_modules/amazon-ivs-player/dist/index.js
  var require_dist = __commonJS({
    "/tmp/ivs-build/node_modules/amazon-ivs-player/dist/index.js"(exports, module) {
      !(function() {
        var e = { 223: function(e2) {
          e2.exports = (function() {
            var e3 = { 228: function(e4) {
              "use strict";
              var t3 = Object.prototype.hasOwnProperty, r3 = "~";
              function n2() {
              }
              function i(e5, t4, r4) {
                this.fn = e5, this.context = t4, this.once = r4 || false;
              }
              function o(e5, t4, n3, o2, a2) {
                if ("function" != typeof n3) throw new TypeError("The listener must be a function");
                var s2 = new i(n3, o2 || e5, a2), u = r3 ? r3 + t4 : t4;
                return e5._events[u] ? e5._events[u].fn ? e5._events[u] = [e5._events[u], s2] : e5._events[u].push(s2) : (e5._events[u] = s2, e5._eventsCount++), e5;
              }
              function a(e5, t4) {
                0 === --e5._eventsCount ? e5._events = new n2() : delete e5._events[t4];
              }
              function s() {
                this._events = new n2(), this._eventsCount = 0;
              }
              Object.create && (n2.prototype = /* @__PURE__ */ Object.create(null), new n2().__proto__ || (r3 = false)), s.prototype.eventNames = function() {
                var e5, n3, i2 = [];
                if (0 === this._eventsCount) return i2;
                for (n3 in e5 = this._events) t3.call(e5, n3) && i2.push(r3 ? n3.slice(1) : n3);
                return Object.getOwnPropertySymbols ? i2.concat(Object.getOwnPropertySymbols(e5)) : i2;
              }, s.prototype.listeners = function(e5) {
                var t4 = r3 ? r3 + e5 : e5, n3 = this._events[t4];
                if (!n3) return [];
                if (n3.fn) return [n3.fn];
                for (var i2 = 0, o2 = n3.length, a2 = new Array(o2); i2 < o2; i2++) a2[i2] = n3[i2].fn;
                return a2;
              }, s.prototype.listenerCount = function(e5) {
                var t4 = r3 ? r3 + e5 : e5, n3 = this._events[t4];
                return n3 ? n3.fn ? 1 : n3.length : 0;
              }, s.prototype.emit = function(e5, t4, n3, i2, o2, a2) {
                var s2 = r3 ? r3 + e5 : e5;
                if (!this._events[s2]) return false;
                var u, c, l = this._events[s2], d = arguments.length;
                if (l.fn) {
                  switch (l.once && this.removeListener(e5, l.fn, void 0, true), d) {
                    case 1:
                      return l.fn.call(l.context), true;
                    case 2:
                      return l.fn.call(l.context, t4), true;
                    case 3:
                      return l.fn.call(l.context, t4, n3), true;
                    case 4:
                      return l.fn.call(l.context, t4, n3, i2), true;
                    case 5:
                      return l.fn.call(l.context, t4, n3, i2, o2), true;
                    case 6:
                      return l.fn.call(l.context, t4, n3, i2, o2, a2), true;
                  }
                  for (c = 1, u = new Array(d - 1); c < d; c++) u[c - 1] = arguments[c];
                  l.fn.apply(l.context, u);
                } else {
                  var f, h = l.length;
                  for (c = 0; c < h; c++) switch (l[c].once && this.removeListener(e5, l[c].fn, void 0, true), d) {
                    case 1:
                      l[c].fn.call(l[c].context);
                      break;
                    case 2:
                      l[c].fn.call(l[c].context, t4);
                      break;
                    case 3:
                      l[c].fn.call(l[c].context, t4, n3);
                      break;
                    case 4:
                      l[c].fn.call(l[c].context, t4, n3, i2);
                      break;
                    default:
                      if (!u) for (f = 1, u = new Array(d - 1); f < d; f++) u[f - 1] = arguments[f];
                      l[c].fn.apply(l[c].context, u);
                  }
                }
                return true;
              }, s.prototype.on = function(e5, t4, r4) {
                return o(this, e5, t4, r4, false);
              }, s.prototype.once = function(e5, t4, r4) {
                return o(this, e5, t4, r4, true);
              }, s.prototype.removeListener = function(e5, t4, n3, i2) {
                var o2 = r3 ? r3 + e5 : e5;
                if (!this._events[o2]) return this;
                if (!t4) return a(this, o2), this;
                var s2 = this._events[o2];
                if (s2.fn) s2.fn !== t4 || i2 && !s2.once || n3 && s2.context !== n3 || a(this, o2);
                else {
                  for (var u = 0, c = [], l = s2.length; u < l; u++) (s2[u].fn !== t4 || i2 && !s2[u].once || n3 && s2[u].context !== n3) && c.push(s2[u]);
                  c.length ? this._events[o2] = 1 === c.length ? c[0] : c : a(this, o2);
                }
                return this;
              }, s.prototype.removeAllListeners = function(e5) {
                var t4;
                return e5 ? (t4 = r3 ? r3 + e5 : e5, this._events[t4] && a(this, t4)) : (this._events = new n2(), this._eventsCount = 0), this;
              }, s.prototype.off = s.prototype.removeListener, s.prototype.addListener = s.prototype.on, s.prefixed = r3, s.EventEmitter = s, e4.exports = s;
            }, 1549: function(e4, t3, r3) {
              var n2 = r3(2032), i = r3(3862), o = r3(6721), a = r3(2749), s = r3(5749);
              function u(e5) {
                var t4 = -1, r4 = null == e5 ? 0 : e5.length;
                for (this.clear(); ++t4 < r4; ) {
                  var n3 = e5[t4];
                  this.set(n3[0], n3[1]);
                }
              }
              u.prototype.clear = n2, u.prototype.delete = i, u.prototype.get = o, u.prototype.has = a, u.prototype.set = s, e4.exports = u;
            }, 79: function(e4, t3, r3) {
              var n2 = r3(3702), i = r3(80), o = r3(4739), a = r3(8655), s = r3(1175);
              function u(e5) {
                var t4 = -1, r4 = null == e5 ? 0 : e5.length;
                for (this.clear(); ++t4 < r4; ) {
                  var n3 = e5[t4];
                  this.set(n3[0], n3[1]);
                }
              }
              u.prototype.clear = n2, u.prototype.delete = i, u.prototype.get = o, u.prototype.has = a, u.prototype.set = s, e4.exports = u;
            }, 8223: function(e4, t3, r3) {
              var n2 = r3(6110)(r3(9325), "Map");
              e4.exports = n2;
            }, 3661: function(e4, t3, r3) {
              var n2 = r3(3040), i = r3(7670), o = r3(289), a = r3(4509), s = r3(2949);
              function u(e5) {
                var t4 = -1, r4 = null == e5 ? 0 : e5.length;
                for (this.clear(); ++t4 < r4; ) {
                  var n3 = e5[t4];
                  this.set(n3[0], n3[1]);
                }
              }
              u.prototype.clear = n2, u.prototype.delete = i, u.prototype.get = o, u.prototype.has = a, u.prototype.set = s, e4.exports = u;
            }, 7217: function(e4, t3, r3) {
              var n2 = r3(79), i = r3(1420), o = r3(938), a = r3(3605), s = r3(9817), u = r3(945);
              function c(e5) {
                var t4 = this.__data__ = new n2(e5);
                this.size = t4.size;
              }
              c.prototype.clear = i, c.prototype.delete = o, c.prototype.get = a, c.prototype.has = s, c.prototype.set = u, e4.exports = c;
            }, 1873: function(e4, t3, r3) {
              var n2 = r3(9325).Symbol;
              e4.exports = n2;
            }, 7828: function(e4, t3, r3) {
              var n2 = r3(9325).Uint8Array;
              e4.exports = n2;
            }, 1033: function(e4) {
              e4.exports = function(e5, t3, r3) {
                switch (r3.length) {
                  case 0:
                    return e5.call(t3);
                  case 1:
                    return e5.call(t3, r3[0]);
                  case 2:
                    return e5.call(t3, r3[0], r3[1]);
                  case 3:
                    return e5.call(t3, r3[0], r3[1], r3[2]);
                }
                return e5.apply(t3, r3);
              };
            }, 695: function(e4, t3, r3) {
              var n2 = r3(8096), i = r3(2428), o = r3(6449), a = r3(3656), s = r3(361), u = r3(7167), c = Object.prototype.hasOwnProperty;
              e4.exports = function(e5, t4) {
                var r4 = o(e5), l = !r4 && i(e5), d = !r4 && !l && a(e5), f = !r4 && !l && !d && u(e5), h = r4 || l || d || f, p = h ? n2(e5.length, String) : [], v = p.length;
                for (var g in e5) !t4 && !c.call(e5, g) || h && ("length" == g || d && ("offset" == g || "parent" == g) || f && ("buffer" == g || "byteLength" == g || "byteOffset" == g) || s(g, v)) || p.push(g);
                return p;
              };
            }, 7805: function(e4, t3, r3) {
              var n2 = r3(3360), i = r3(5288);
              e4.exports = function(e5, t4, r4) {
                (void 0 !== r4 && !i(e5[t4], r4) || void 0 === r4 && !(t4 in e5)) && n2(e5, t4, r4);
              };
            }, 6547: function(e4, t3, r3) {
              var n2 = r3(3360), i = r3(5288), o = Object.prototype.hasOwnProperty;
              e4.exports = function(e5, t4, r4) {
                var a = e5[t4];
                o.call(e5, t4) && i(a, r4) && (void 0 !== r4 || t4 in e5) || n2(e5, t4, r4);
              };
            }, 6025: function(e4, t3, r3) {
              var n2 = r3(5288);
              e4.exports = function(e5, t4) {
                for (var r4 = e5.length; r4--; ) if (n2(e5[r4][0], t4)) return r4;
                return -1;
              };
            }, 3360: function(e4, t3, r3) {
              var n2 = r3(3243);
              e4.exports = function(e5, t4, r4) {
                "__proto__" == t4 && n2 ? n2(e5, t4, { configurable: true, enumerable: true, value: r4, writable: true }) : e5[t4] = r4;
              };
            }, 9344: function(e4, t3, r3) {
              var n2 = r3(3805), i = Object.create, o = /* @__PURE__ */ (function() {
                function e5() {
                }
                return function(t4) {
                  if (!n2(t4)) return {};
                  if (i) return i(t4);
                  e5.prototype = t4;
                  var r4 = new e5();
                  return e5.prototype = void 0, r4;
                };
              })();
              e4.exports = o;
            }, 6649: function(e4, t3, r3) {
              var n2 = r3(3221)();
              e4.exports = n2;
            }, 2552: function(e4, t3, r3) {
              var n2 = r3(1873), i = r3(659), o = r3(9350), a = n2 ? n2.toStringTag : void 0;
              e4.exports = function(e5) {
                return null == e5 ? void 0 === e5 ? "[object Undefined]" : "[object Null]" : a && a in Object(e5) ? i(e5) : o(e5);
              };
            }, 7534: function(e4, t3, r3) {
              var n2 = r3(2552), i = r3(346);
              e4.exports = function(e5) {
                return i(e5) && "[object Arguments]" == n2(e5);
              };
            }, 5083: function(e4, t3, r3) {
              var n2 = r3(1882), i = r3(7296), o = r3(3805), a = r3(7473), s = /^\[object .+?Constructor\]$/, u = Function.prototype, c = Object.prototype, l = u.toString, d = c.hasOwnProperty, f = RegExp("^" + l.call(d).replace(/[\\^$.*+?()[\]{}|]/g, "\\$&").replace(/hasOwnProperty|(function).*?(?=\\\()| for .+?(?=\\\])/g, "$1.*?") + "$");
              e4.exports = function(e5) {
                return !(!o(e5) || i(e5)) && (n2(e5) ? f : s).test(a(e5));
              };
            }, 4901: function(e4, t3, r3) {
              var n2 = r3(2552), i = r3(294), o = r3(346), a = {};
              a["[object Float32Array]"] = a["[object Float64Array]"] = a["[object Int8Array]"] = a["[object Int16Array]"] = a["[object Int32Array]"] = a["[object Uint8Array]"] = a["[object Uint8ClampedArray]"] = a["[object Uint16Array]"] = a["[object Uint32Array]"] = true, a["[object Arguments]"] = a["[object Array]"] = a["[object ArrayBuffer]"] = a["[object Boolean]"] = a["[object DataView]"] = a["[object Date]"] = a["[object Error]"] = a["[object Function]"] = a["[object Map]"] = a["[object Number]"] = a["[object Object]"] = a["[object RegExp]"] = a["[object Set]"] = a["[object String]"] = a["[object WeakMap]"] = false, e4.exports = function(e5) {
                return o(e5) && i(e5.length) && !!a[n2(e5)];
              };
            }, 2903: function(e4, t3, r3) {
              var n2 = r3(3805), i = r3(5527), o = r3(181), a = Object.prototype.hasOwnProperty;
              e4.exports = function(e5) {
                if (!n2(e5)) return o(e5);
                var t4 = i(e5), r4 = [];
                for (var s in e5) ("constructor" != s || !t4 && a.call(e5, s)) && r4.push(s);
                return r4;
              };
            }, 5250: function(e4, t3, r3) {
              var n2 = r3(7217), i = r3(7805), o = r3(6649), a = r3(2824), s = r3(3805), u = r3(7241), c = r3(4974);
              e4.exports = function e5(t4, r4, l, d, f) {
                t4 !== r4 && o(r4, function(o2, u2) {
                  if (f || (f = new n2()), s(o2)) a(t4, r4, u2, l, e5, d, f);
                  else {
                    var h = d ? d(c(t4, u2), o2, u2 + "", t4, r4, f) : void 0;
                    void 0 === h && (h = o2), i(t4, u2, h);
                  }
                }, u);
              };
            }, 2824: function(e4, t3, r3) {
              var n2 = r3(7805), i = r3(3290), o = r3(1961), a = r3(3007), s = r3(5529), u = r3(2428), c = r3(6449), l = r3(3693), d = r3(3656), f = r3(1882), h = r3(3805), p = r3(1331), v = r3(7167), g = r3(4974), m = r3(9884);
              e4.exports = function(e5, t4, r4, y, b, E, S) {
                var T = g(e5, r4), _ = g(t4, r4), C = S.get(_);
                if (C) n2(e5, r4, C);
                else {
                  var k = E ? E(T, _, r4 + "", e5, t4, S) : void 0, w = void 0 === k;
                  if (w) {
                    var P = c(_), A = !P && d(_), I = !P && !A && v(_);
                    k = _, P || A || I ? c(T) ? k = T : l(T) ? k = a(T) : A ? (w = false, k = i(_, true)) : I ? (w = false, k = o(_, true)) : k = [] : p(_) || u(_) ? (k = T, u(T) ? k = m(T) : h(T) && !f(T) || (k = s(_))) : w = false;
                  }
                  w && (S.set(_, k), b(k, _, y, E, S), S.delete(_)), n2(e5, r4, k);
                }
              };
            }, 9302: function(e4, t3, r3) {
              var n2 = r3(3488), i = r3(6757), o = r3(2865);
              e4.exports = function(e5, t4) {
                return o(i(e5, t4, n2), e5 + "");
              };
            }, 9570: function(e4, t3, r3) {
              var n2 = r3(7334), i = r3(3243), o = r3(3488), a = i ? function(e5, t4) {
                return i(e5, "toString", { configurable: true, enumerable: false, value: n2(t4), writable: true });
              } : o;
              e4.exports = a;
            }, 8096: function(e4) {
              e4.exports = function(e5, t3) {
                for (var r3 = -1, n2 = Array(e5); ++r3 < e5; ) n2[r3] = t3(r3);
                return n2;
              };
            }, 7301: function(e4) {
              e4.exports = function(e5) {
                return function(t3) {
                  return e5(t3);
                };
              };
            }, 9653: function(e4, t3, r3) {
              var n2 = r3(7828);
              e4.exports = function(e5) {
                var t4 = new e5.constructor(e5.byteLength);
                return new n2(t4).set(new n2(e5)), t4;
              };
            }, 3290: function(e4, t3, r3) {
              e4 = r3.nmd(e4);
              var n2 = r3(9325), i = t3 && !t3.nodeType && t3, o = i && e4 && !e4.nodeType && e4, a = o && o.exports === i ? n2.Buffer : void 0, s = a ? a.allocUnsafe : void 0;
              e4.exports = function(e5, t4) {
                if (t4) return e5.slice();
                var r4 = e5.length, n3 = s ? s(r4) : new e5.constructor(r4);
                return e5.copy(n3), n3;
              };
            }, 1961: function(e4, t3, r3) {
              var n2 = r3(9653);
              e4.exports = function(e5, t4) {
                var r4 = t4 ? n2(e5.buffer) : e5.buffer;
                return new e5.constructor(r4, e5.byteOffset, e5.length);
              };
            }, 3007: function(e4) {
              e4.exports = function(e5, t3) {
                var r3 = -1, n2 = e5.length;
                for (t3 || (t3 = Array(n2)); ++r3 < n2; ) t3[r3] = e5[r3];
                return t3;
              };
            }, 1791: function(e4, t3, r3) {
              var n2 = r3(6547), i = r3(3360);
              e4.exports = function(e5, t4, r4, o) {
                var a = !r4;
                r4 || (r4 = {});
                for (var s = -1, u = t4.length; ++s < u; ) {
                  var c = t4[s], l = o ? o(r4[c], e5[c], c, r4, e5) : void 0;
                  void 0 === l && (l = e5[c]), a ? i(r4, c, l) : n2(r4, c, l);
                }
                return r4;
              };
            }, 5481: function(e4, t3, r3) {
              var n2 = r3(9325)["__core-js_shared__"];
              e4.exports = n2;
            }, 999: function(e4, t3, r3) {
              var n2 = r3(9302), i = r3(6800);
              e4.exports = function(e5) {
                return n2(function(t4, r4) {
                  var n3 = -1, o = r4.length, a = o > 1 ? r4[o - 1] : void 0, s = o > 2 ? r4[2] : void 0;
                  for (a = e5.length > 3 && "function" == typeof a ? (o--, a) : void 0, s && i(r4[0], r4[1], s) && (a = o < 3 ? void 0 : a, o = 1), t4 = Object(t4); ++n3 < o; ) {
                    var u = r4[n3];
                    u && e5(t4, u, n3, a);
                  }
                  return t4;
                });
              };
            }, 3221: function(e4) {
              e4.exports = function(e5) {
                return function(t3, r3, n2) {
                  for (var i = -1, o = Object(t3), a = n2(t3), s = a.length; s--; ) {
                    var u = a[e5 ? s : ++i];
                    if (false === r3(o[u], u, o)) break;
                  }
                  return t3;
                };
              };
            }, 3243: function(e4, t3, r3) {
              var n2 = r3(6110), i = (function() {
                try {
                  var e5 = n2(Object, "defineProperty");
                  return e5({}, "", {}), e5;
                } catch (e6) {
                }
              })();
              e4.exports = i;
            }, 4840: function(e4, t3, r3) {
              var n2 = "object" == typeof r3.g && r3.g && r3.g.Object === Object && r3.g;
              e4.exports = n2;
            }, 2651: function(e4, t3, r3) {
              var n2 = r3(4218);
              e4.exports = function(e5, t4) {
                var r4 = e5.__data__;
                return n2(t4) ? r4["string" == typeof t4 ? "string" : "hash"] : r4.map;
              };
            }, 6110: function(e4, t3, r3) {
              var n2 = r3(5083), i = r3(392);
              e4.exports = function(e5, t4) {
                var r4 = i(e5, t4);
                return n2(r4) ? r4 : void 0;
              };
            }, 8879: function(e4, t3, r3) {
              var n2 = r3(4335)(Object.getPrototypeOf, Object);
              e4.exports = n2;
            }, 659: function(e4, t3, r3) {
              var n2 = r3(1873), i = Object.prototype, o = i.hasOwnProperty, a = i.toString, s = n2 ? n2.toStringTag : void 0;
              e4.exports = function(e5) {
                var t4 = o.call(e5, s), r4 = e5[s];
                try {
                  e5[s] = void 0;
                  var n3 = true;
                } catch (e6) {
                }
                var i2 = a.call(e5);
                return n3 && (t4 ? e5[s] = r4 : delete e5[s]), i2;
              };
            }, 392: function(e4) {
              e4.exports = function(e5, t3) {
                return null == e5 ? void 0 : e5[t3];
              };
            }, 2032: function(e4, t3, r3) {
              var n2 = r3(1042);
              e4.exports = function() {
                this.__data__ = n2 ? n2(null) : {}, this.size = 0;
              };
            }, 3862: function(e4) {
              e4.exports = function(e5) {
                var t3 = this.has(e5) && delete this.__data__[e5];
                return this.size -= t3 ? 1 : 0, t3;
              };
            }, 6721: function(e4, t3, r3) {
              var n2 = r3(1042), i = Object.prototype.hasOwnProperty;
              e4.exports = function(e5) {
                var t4 = this.__data__;
                if (n2) {
                  var r4 = t4[e5];
                  return "__lodash_hash_undefined__" === r4 ? void 0 : r4;
                }
                return i.call(t4, e5) ? t4[e5] : void 0;
              };
            }, 2749: function(e4, t3, r3) {
              var n2 = r3(1042), i = Object.prototype.hasOwnProperty;
              e4.exports = function(e5) {
                var t4 = this.__data__;
                return n2 ? void 0 !== t4[e5] : i.call(t4, e5);
              };
            }, 5749: function(e4, t3, r3) {
              var n2 = r3(1042);
              e4.exports = function(e5, t4) {
                var r4 = this.__data__;
                return this.size += this.has(e5) ? 0 : 1, r4[e5] = n2 && void 0 === t4 ? "__lodash_hash_undefined__" : t4, this;
              };
            }, 5529: function(e4, t3, r3) {
              var n2 = r3(9344), i = r3(8879), o = r3(5527);
              e4.exports = function(e5) {
                return "function" != typeof e5.constructor || o(e5) ? {} : n2(i(e5));
              };
            }, 361: function(e4) {
              var t3 = /^(?:0|[1-9]\d*)$/;
              e4.exports = function(e5, r3) {
                var n2 = typeof e5;
                return !!(r3 = null == r3 ? 9007199254740991 : r3) && ("number" == n2 || "symbol" != n2 && t3.test(e5)) && e5 > -1 && e5 % 1 == 0 && e5 < r3;
              };
            }, 6800: function(e4, t3, r3) {
              var n2 = r3(5288), i = r3(4894), o = r3(361), a = r3(3805);
              e4.exports = function(e5, t4, r4) {
                if (!a(r4)) return false;
                var s = typeof t4;
                return !!("number" == s ? i(r4) && o(t4, r4.length) : "string" == s && t4 in r4) && n2(r4[t4], e5);
              };
            }, 4218: function(e4) {
              e4.exports = function(e5) {
                var t3 = typeof e5;
                return "string" == t3 || "number" == t3 || "symbol" == t3 || "boolean" == t3 ? "__proto__" !== e5 : null === e5;
              };
            }, 7296: function(e4, t3, r3) {
              var n2, i = r3(5481), o = (n2 = /[^.]+$/.exec(i && i.keys && i.keys.IE_PROTO || "")) ? "Symbol(src)_1." + n2 : "";
              e4.exports = function(e5) {
                return !!o && o in e5;
              };
            }, 5527: function(e4) {
              var t3 = Object.prototype;
              e4.exports = function(e5) {
                var r3 = e5 && e5.constructor;
                return e5 === ("function" == typeof r3 && r3.prototype || t3);
              };
            }, 3702: function(e4) {
              e4.exports = function() {
                this.__data__ = [], this.size = 0;
              };
            }, 80: function(e4, t3, r3) {
              var n2 = r3(6025), i = Array.prototype.splice;
              e4.exports = function(e5) {
                var t4 = this.__data__, r4 = n2(t4, e5);
                return !(r4 < 0 || (r4 == t4.length - 1 ? t4.pop() : i.call(t4, r4, 1), --this.size, 0));
              };
            }, 4739: function(e4, t3, r3) {
              var n2 = r3(6025);
              e4.exports = function(e5) {
                var t4 = this.__data__, r4 = n2(t4, e5);
                return r4 < 0 ? void 0 : t4[r4][1];
              };
            }, 8655: function(e4, t3, r3) {
              var n2 = r3(6025);
              e4.exports = function(e5) {
                return n2(this.__data__, e5) > -1;
              };
            }, 1175: function(e4, t3, r3) {
              var n2 = r3(6025);
              e4.exports = function(e5, t4) {
                var r4 = this.__data__, i = n2(r4, e5);
                return i < 0 ? (++this.size, r4.push([e5, t4])) : r4[i][1] = t4, this;
              };
            }, 3040: function(e4, t3, r3) {
              var n2 = r3(1549), i = r3(79), o = r3(8223);
              e4.exports = function() {
                this.size = 0, this.__data__ = { hash: new n2(), map: new (o || i)(), string: new n2() };
              };
            }, 7670: function(e4, t3, r3) {
              var n2 = r3(2651);
              e4.exports = function(e5) {
                var t4 = n2(this, e5).delete(e5);
                return this.size -= t4 ? 1 : 0, t4;
              };
            }, 289: function(e4, t3, r3) {
              var n2 = r3(2651);
              e4.exports = function(e5) {
                return n2(this, e5).get(e5);
              };
            }, 4509: function(e4, t3, r3) {
              var n2 = r3(2651);
              e4.exports = function(e5) {
                return n2(this, e5).has(e5);
              };
            }, 2949: function(e4, t3, r3) {
              var n2 = r3(2651);
              e4.exports = function(e5, t4) {
                var r4 = n2(this, e5), i = r4.size;
                return r4.set(e5, t4), this.size += r4.size == i ? 0 : 1, this;
              };
            }, 1042: function(e4, t3, r3) {
              var n2 = r3(6110)(Object, "create");
              e4.exports = n2;
            }, 181: function(e4) {
              e4.exports = function(e5) {
                var t3 = [];
                if (null != e5) for (var r3 in Object(e5)) t3.push(r3);
                return t3;
              };
            }, 6009: function(e4, t3, r3) {
              e4 = r3.nmd(e4);
              var n2 = r3(4840), i = t3 && !t3.nodeType && t3, o = i && e4 && !e4.nodeType && e4, a = o && o.exports === i && n2.process, s = (function() {
                try {
                  return o && o.require && o.require("util").types || a && a.binding && a.binding("util");
                } catch (e5) {
                }
              })();
              e4.exports = s;
            }, 9350: function(e4) {
              var t3 = Object.prototype.toString;
              e4.exports = function(e5) {
                return t3.call(e5);
              };
            }, 4335: function(e4) {
              e4.exports = function(e5, t3) {
                return function(r3) {
                  return e5(t3(r3));
                };
              };
            }, 6757: function(e4, t3, r3) {
              var n2 = r3(1033), i = Math.max;
              e4.exports = function(e5, t4, r4) {
                return t4 = i(void 0 === t4 ? e5.length - 1 : t4, 0), function() {
                  for (var o = arguments, a = -1, s = i(o.length - t4, 0), u = Array(s); ++a < s; ) u[a] = o[t4 + a];
                  a = -1;
                  for (var c = Array(t4 + 1); ++a < t4; ) c[a] = o[a];
                  return c[t4] = r4(u), n2(e5, this, c);
                };
              };
            }, 9325: function(e4, t3, r3) {
              var n2 = r3(4840), i = "object" == typeof self && self && self.Object === Object && self, o = n2 || i || Function("return this")();
              e4.exports = o;
            }, 4974: function(e4) {
              e4.exports = function(e5, t3) {
                if (("constructor" !== t3 || "function" != typeof e5[t3]) && "__proto__" != t3) return e5[t3];
              };
            }, 2865: function(e4, t3, r3) {
              var n2 = r3(9570), i = r3(1811)(n2);
              e4.exports = i;
            }, 1811: function(e4) {
              var t3 = Date.now;
              e4.exports = function(e5) {
                var r3 = 0, n2 = 0;
                return function() {
                  var i = t3(), o = 16 - (i - n2);
                  if (n2 = i, o > 0) {
                    if (++r3 >= 800) return arguments[0];
                  } else r3 = 0;
                  return e5.apply(void 0, arguments);
                };
              };
            }, 1420: function(e4, t3, r3) {
              var n2 = r3(79);
              e4.exports = function() {
                this.__data__ = new n2(), this.size = 0;
              };
            }, 938: function(e4) {
              e4.exports = function(e5) {
                var t3 = this.__data__, r3 = t3.delete(e5);
                return this.size = t3.size, r3;
              };
            }, 3605: function(e4) {
              e4.exports = function(e5) {
                return this.__data__.get(e5);
              };
            }, 9817: function(e4) {
              e4.exports = function(e5) {
                return this.__data__.has(e5);
              };
            }, 945: function(e4, t3, r3) {
              var n2 = r3(79), i = r3(8223), o = r3(3661);
              e4.exports = function(e5, t4) {
                var r4 = this.__data__;
                if (r4 instanceof n2) {
                  var a = r4.__data__;
                  if (!i || a.length < 199) return a.push([e5, t4]), this.size = ++r4.size, this;
                  r4 = this.__data__ = new o(a);
                }
                return r4.set(e5, t4), this.size = r4.size, this;
              };
            }, 7473: function(e4) {
              var t3 = Function.prototype.toString;
              e4.exports = function(e5) {
                if (null != e5) {
                  try {
                    return t3.call(e5);
                  } catch (e6) {
                  }
                  try {
                    return e5 + "";
                  } catch (e6) {
                  }
                }
                return "";
              };
            }, 7334: function(e4) {
              e4.exports = function(e5) {
                return function() {
                  return e5;
                };
              };
            }, 5288: function(e4) {
              e4.exports = function(e5, t3) {
                return e5 === t3 || e5 != e5 && t3 != t3;
              };
            }, 3488: function(e4) {
              e4.exports = function(e5) {
                return e5;
              };
            }, 2428: function(e4, t3, r3) {
              var n2 = r3(7534), i = r3(346), o = Object.prototype, a = o.hasOwnProperty, s = o.propertyIsEnumerable, u = n2(/* @__PURE__ */ (function() {
                return arguments;
              })()) ? n2 : function(e5) {
                return i(e5) && a.call(e5, "callee") && !s.call(e5, "callee");
              };
              e4.exports = u;
            }, 6449: function(e4) {
              var t3 = Array.isArray;
              e4.exports = t3;
            }, 4894: function(e4, t3, r3) {
              var n2 = r3(1882), i = r3(294);
              e4.exports = function(e5) {
                return null != e5 && i(e5.length) && !n2(e5);
              };
            }, 3693: function(e4, t3, r3) {
              var n2 = r3(4894), i = r3(346);
              e4.exports = function(e5) {
                return i(e5) && n2(e5);
              };
            }, 3656: function(e4, t3, r3) {
              e4 = r3.nmd(e4);
              var n2 = r3(9325), i = r3(9935), o = t3 && !t3.nodeType && t3, a = o && e4 && !e4.nodeType && e4, s = a && a.exports === o ? n2.Buffer : void 0, u = (s ? s.isBuffer : void 0) || i;
              e4.exports = u;
            }, 1882: function(e4, t3, r3) {
              var n2 = r3(2552), i = r3(3805);
              e4.exports = function(e5) {
                if (!i(e5)) return false;
                var t4 = n2(e5);
                return "[object Function]" == t4 || "[object GeneratorFunction]" == t4 || "[object AsyncFunction]" == t4 || "[object Proxy]" == t4;
              };
            }, 294: function(e4) {
              e4.exports = function(e5) {
                return "number" == typeof e5 && e5 > -1 && e5 % 1 == 0 && e5 <= 9007199254740991;
              };
            }, 3805: function(e4) {
              e4.exports = function(e5) {
                var t3 = typeof e5;
                return null != e5 && ("object" == t3 || "function" == t3);
              };
            }, 346: function(e4) {
              e4.exports = function(e5) {
                return null != e5 && "object" == typeof e5;
              };
            }, 1331: function(e4, t3, r3) {
              var n2 = r3(2552), i = r3(8879), o = r3(346), a = Function.prototype, s = Object.prototype, u = a.toString, c = s.hasOwnProperty, l = u.call(Object);
              e4.exports = function(e5) {
                if (!o(e5) || "[object Object]" != n2(e5)) return false;
                var t4 = i(e5);
                if (null === t4) return true;
                var r4 = c.call(t4, "constructor") && t4.constructor;
                return "function" == typeof r4 && r4 instanceof r4 && u.call(r4) == l;
              };
            }, 7167: function(e4, t3, r3) {
              var n2 = r3(4901), i = r3(7301), o = r3(6009), a = o && o.isTypedArray, s = a ? i(a) : n2;
              e4.exports = s;
            }, 7241: function(e4, t3, r3) {
              var n2 = r3(695), i = r3(2903), o = r3(4894);
              e4.exports = function(e5) {
                return o(e5) ? n2(e5, true) : i(e5);
              };
            }, 6924: function(e4, t3, r3) {
              var n2 = r3(5250), i = r3(999)(function(e5, t4, r4, i2) {
                n2(e5, t4, r4, i2);
              });
              e4.exports = i;
            }, 9935: function(e4) {
              e4.exports = function() {
                return false;
              };
            }, 9884: function(e4, t3, r3) {
              var n2 = r3(1791), i = r3(7241);
              e4.exports = function(e5) {
                return n2(e5, i(e5));
              };
            }, 4355: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.CriteriaInputs = t3.BooleanComparisonSchema = void 0;
              var n2 = r3(4713);
              t3.BooleanComparisonSchema = { comparison: { type: "string", required: true }, format: { type: "string", required: true }, value: { type: "string", required: false } };
              var i = (function() {
                function e5(e6) {
                  this.inputs = e6;
                }
                return e5.prototype.matches = function(e6, t4) {
                  if (0 === t4.length) return { success: true, matched: false };
                  var r4 = this.inputs[e6];
                  if (void 0 === r4) return { success: true, matched: false };
                  for (var n3 = 0, i2 = t4; n3 < i2.length; n3++) {
                    var o = i2[n3], a = this.matchSingleFilter(e6, r4, o);
                    if (!a.success) return a;
                    if (a.matched) return { success: true, matched: true };
                  }
                  return { success: true, matched: false };
                }, e5.prototype.matchSingleFilter = function(e6, t4, r4) {
                  return this.isPrimitiveFilter(r4) ? this.matchPrimitive(t4, r4) : this.isComparatorFilter(r4) ? this.matchComparator(e6, t4, r4) : { success: false, error: "Invalid filter type for property ".concat(e6) };
                }, e5.prototype.matchPrimitive = function(e6, t4) {
                  return typeof e6 != typeof t4 ? { success: true, matched: false } : "string" == typeof e6 && "string" == typeof t4 ? { success: true, matched: (0, n2.matchesWithWildcard)(e6, t4) } : { success: true, matched: e6 === t4 };
                }, e5.prototype.matchComparator = function(e6, t4, r4) {
                  var n3 = r4.comparison, i2 = r4.format, o = r4.value;
                  return this.isValidComparisonOperator(n3) ? "semver" === i2 ? this.matchSemverComparator(t4, n3, o) : "number" === i2 ? this.matchNumberComparator(t4, n3, o) : { success: false, error: "Unknown comparator format: ".concat(i2) } : { success: false, error: "Invalid comparison operator: ".concat(n3) };
                }, e5.prototype.matchSemverComparator = function(e6, t4, r4) {
                  if ("string" != typeof e6) return { success: false, error: "Input value must be string for semver comparison" };
                  var i2 = (0, n2.compareSemvers)(e6, r4);
                  return void 0 === i2 ? { success: false, error: 'Invalid semver: input="'.concat(e6, '", filter="').concat(r4, '"') } : { success: true, matched: this.evaluateComparison(i2, t4) };
                }, e5.prototype.matchNumberComparator = function(e6, t4, r4) {
                  if ("number" != typeof e6) return { success: false, error: "Input value must be number for number comparison" };
                  var n3, i2 = parseFloat(r4);
                  return isNaN(i2) ? { success: false, error: "Invalid number in filter: ".concat(r4) } : (n3 = e6 < i2 ? -1 : e6 > i2 ? 1 : 0, { success: true, matched: this.evaluateComparison(n3, t4) });
                }, e5.prototype.evaluateComparison = function(e6, t4) {
                  switch (t4) {
                    case "<":
                      return e6 < 0;
                    case "<=":
                      return e6 <= 0;
                    case ">":
                      return e6 > 0;
                    case ">=":
                      return e6 >= 0;
                    case "==":
                      return 0 === e6;
                    case "!=":
                      return 0 !== e6;
                    default:
                      return false;
                  }
                }, e5.prototype.isPrimitiveFilter = function(e6) {
                  return "string" == typeof e6 || "number" == typeof e6 || "boolean" == typeof e6;
                }, e5.prototype.isComparatorFilter = function(e6) {
                  return "object" == typeof e6 && null !== e6 && "comparison" in e6 && "format" in e6 && "value" in e6;
                }, e5.prototype.isValidComparisonOperator = function(e6) {
                  return ["<", "<=", ">", ">=", "==", "!="].includes(e6);
                }, e5;
              })();
              t3.CriteriaInputs = i;
            }, 5185: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.CriteriaParser = t3.DeviceConfigCriteriaConfigSchema = t3.DeviceConfigCriteriaSchema = t3.DeviceConfigRulesSchema = void 0;
              var n2 = r3(4293);
              t3.DeviceConfigRulesSchema = {}, t3.DeviceConfigCriteriaSchema = { version: { type: "number", required: true }, rules: { type: "object", required: true, config: { schema: t3.DeviceConfigRulesSchema } } }, t3.DeviceConfigCriteriaConfigSchema = { criteria: { type: "object", required: true, config: { schema: t3.DeviceConfigCriteriaSchema } }, payload: { type: "object", required: true, config: { schema: {} } } };
              var i = (function() {
                function e5() {
                }
                return Object.defineProperty(e5, "CURRENT_VERSION", { get: function() {
                  return this.SUPPORTED_VERSION;
                }, enumerable: false, configurable: true }), e5.evaluateCriteria = function(e6, t4) {
                  if (!e6.criteria) return (0, n2.err)("Missing criteria object");
                  var r4 = e6.criteria;
                  if (r4.version !== this.SUPPORTED_VERSION) return (0, n2.ok)({ matched: false });
                  if (!r4.rules) return (0, n2.err)("Missing rules in criteria");
                  var i2 = r4.rules;
                  if (0 === Object.keys(i2).length) return (0, n2.ok)({ matched: true, payload: e6.payload });
                  for (var o = 0, a = Object.entries(i2); o < a.length; o++) {
                    var s = a[o], u = s[0], c = s[1], l = t4.matches(u, c);
                    if (!l.success) return (0, n2.err)(l.error || "Match evaluation failed");
                    if (!l.matched) return (0, n2.ok)({ matched: false });
                  }
                  return (0, n2.ok)({ matched: true, payload: e6.payload });
                }, e5.matchingPayloads = function(e6, t4) {
                  for (var r4 = [], i2 = [], o = 0, a = e6; o < a.length; o++) {
                    var s = a[o], u = this.evaluateCriteria(s, t4);
                    u.ok ? u.value.matched && void 0 !== u.value.payload && r4.push(u.value.payload) : i2.push(u.error);
                  }
                  return i2.length > 0 ? (0, n2.err)(i2) : (0, n2.ok)({ payloads: r4 });
                }, e5.matches = function(e6, t4, r4) {
                  return r4.matches(e6, t4);
                }, e5.SUPPORTED_VERSION = 1, e5;
              })();
              t3.CriteriaParser = i;
            }, 4713: function(e4, t3) {
              "use strict";
              function r3(e5) {
                if (e5 && "string" == typeof e5) {
                  var t4 = e5.split("+")[0];
                  if (t4) {
                    var r4 = t4.split("-"), n2 = r4[0], i = r4.slice(1);
                    if (n2) {
                      var o = n2.split(".");
                      if (!(0 === o.length || o.length > 3 || o.some(function(e6) {
                        return "" === e6 || !/^\d+$/.test(e6);
                      }))) {
                        var a = o.map(function(e6) {
                          return parseInt(e6, 10);
                        }), s = a[0], u = void 0 === s ? 0 : s, c = a[1], l = void 0 === c ? 0 : c, d = a[2], f = { major: u, minor: l, patch: void 0 === d ? 0 : d };
                        if (i.length > 0) {
                          var h = i.join("-");
                          h && (f.prerelease = h.split("."));
                        }
                        return f;
                      }
                    }
                  }
                }
              }
              Object.defineProperty(t3, "__esModule", { value: true }), t3.compareSemvers = t3.parseSemver = t3.matchesWithWildcard = void 0, t3.matchesWithWildcard = function(e5, t4) {
                if ("*" === t4) return true;
                if (t4.endsWith("*")) {
                  var r4 = t4.slice(0, -1);
                  return 0 === e5.indexOf(r4);
                }
                return e5 === t4;
              }, t3.parseSemver = r3, t3.compareSemvers = function(e5, t4) {
                var n2 = r3(e5), i = r3(t4);
                if (n2 && i) {
                  if (n2.major !== i.major) return n2.major > i.major ? 1 : -1;
                  if (n2.minor !== i.minor) return n2.minor > i.minor ? 1 : -1;
                  if (n2.patch !== i.patch) return n2.patch > i.patch ? 1 : -1;
                  var o = n2.prerelease && n2.prerelease.length > 0, a = i.prerelease && i.prerelease.length > 0;
                  if (o && !a) return -1;
                  if (!o && a) return 1;
                  if (!o && !a) return 0;
                  for (var s = n2.prerelease, u = i.prerelease, c = Math.max(s.length, u.length), l = 0; l < c; l++) {
                    var d = s[l], f = u[l];
                    if (void 0 === d) return -1;
                    if (void 0 === f) return 1;
                    var h = /^\d+$/.test(d), p = /^\d+$/.test(f);
                    if (h && p) {
                      var v = parseInt(d, 10), g = parseInt(f, 10);
                      if (v !== g) return v > g ? 1 : -1;
                    } else {
                      if (h && !p) return -1;
                      if (!h && p) return 1;
                      if (d !== f) return d > f ? 1 : -1;
                    }
                  }
                  return 0;
                }
              };
            }, 6270: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.DeviceConfigRulesSchema = t3.DeviceConfigCriteriaSchema = t3.DeviceConfigCriteriaConfigSchema = t3.CriteriaParser = t3.BooleanComparisonSchema = t3.CriteriaInputs = t3.compareSemvers = t3.parseSemver = t3.matchesWithWildcard = void 0;
              var n2 = r3(4713);
              Object.defineProperty(t3, "matchesWithWildcard", { enumerable: true, get: function() {
                return n2.matchesWithWildcard;
              } }), Object.defineProperty(t3, "parseSemver", { enumerable: true, get: function() {
                return n2.parseSemver;
              } }), Object.defineProperty(t3, "compareSemvers", { enumerable: true, get: function() {
                return n2.compareSemvers;
              } });
              var i = r3(4355);
              Object.defineProperty(t3, "CriteriaInputs", { enumerable: true, get: function() {
                return i.CriteriaInputs;
              } }), Object.defineProperty(t3, "BooleanComparisonSchema", { enumerable: true, get: function() {
                return i.BooleanComparisonSchema;
              } });
              var o = r3(5185);
              Object.defineProperty(t3, "CriteriaParser", { enumerable: true, get: function() {
                return o.CriteriaParser;
              } }), Object.defineProperty(t3, "DeviceConfigCriteriaConfigSchema", { enumerable: true, get: function() {
                return o.DeviceConfigCriteriaConfigSchema;
              } }), Object.defineProperty(t3, "DeviceConfigCriteriaSchema", { enumerable: true, get: function() {
                return o.DeviceConfigCriteriaSchema;
              } }), Object.defineProperty(t3, "DeviceConfigRulesSchema", { enumerable: true, get: function() {
                return o.DeviceConfigRulesSchema;
              } });
            }, 7094: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.DefaultClock = t3.DefaultStorage = t3.DefaultAnalytics = t3.DefaultTrace = t3.getDefaultConfigRefreshOptions = t3.getDefaultCommonAnalytics = t3.DEFAULT_INITIAL_DELAY_SECONDS = t3.DEFAULT_HTTP_TIMEOUT_SECONDS = t3.DEFAULT_STOP_REFRESH_AFTER_SECONDS = t3.DEFAULT_MAX_AGE_CACHE_SECONDS = t3.DEFAULT_RETRY_INTERVAL_SECONDS = t3.DEFAULT_RETRY_COUNT = t3.DEFAULT_REFRESH_INTERVAL_SECONDS = t3.STORAGE_PREFIX_KEY = t3.ENCODING_BASE64 = t3.STATE_KEY = t3.DATA_KEY = void 0;
              var n2 = r3(3259), i = r3(4811);
              t3.DATA_KEY = "data", t3.STATE_KEY = "state", t3.ENCODING_BASE64 = "base64", t3.STORAGE_PREFIX_KEY = "amazon_ivs_device_config_v1_", t3.DEFAULT_REFRESH_INTERVAL_SECONDS = (0, n2.minutesToSeconds)(15), t3.DEFAULT_RETRY_COUNT = 3, t3.DEFAULT_RETRY_INTERVAL_SECONDS = 10, t3.DEFAULT_MAX_AGE_CACHE_SECONDS = (0, n2.daysToSeconds)(3), t3.DEFAULT_STOP_REFRESH_AFTER_SECONDS = (0, n2.hoursToSeconds)(2), t3.DEFAULT_HTTP_TIMEOUT_SECONDS = 15, t3.DEFAULT_INITIAL_DELAY_SECONDS = 5, t3.getDefaultCommonAnalytics = function() {
                return { client_sdk: "unknown", env: i.DeviceConfigEnv.PROD };
              }, t3.getDefaultConfigRefreshOptions = function() {
                return { refreshIntervalSeconds: t3.DEFAULT_REFRESH_INTERVAL_SECONDS, retryCount: t3.DEFAULT_RETRY_COUNT, retryIntervalSeconds: t3.DEFAULT_RETRY_INTERVAL_SECONDS, maxCacheAgeSeconds: t3.DEFAULT_MAX_AGE_CACHE_SECONDS, stopRefreshAfterSeconds: t3.DEFAULT_STOP_REFRESH_AFTER_SECONDS, canRefreshNow: function() {
                  return true;
                } };
              };
              var o = (function() {
                function e5(e6) {
                  this.enableConsoleLog = e6;
                }
                return e5.prototype.onTrace = function(e6) {
                  this.enableConsoleLog && console.log("DeviceConfig trace: " + e6);
                }, e5;
              })();
              t3.DefaultTrace = o;
              var a = (function() {
                function e5(e6) {
                  this.enableConsoleLog = e6;
                }
                return e5.prototype.onValue = function(e6) {
                  this.enableConsoleLog && console.log("DeviceConfig analytics value:  ".concat(e6));
                }, e5.prototype.onError = function(e6) {
                  this.enableConsoleLog && console.error("DeviceConfig analytics error: ".concat(e6));
                }, e5.prototype.onTrace = function(e6) {
                  this.enableConsoleLog && console.log("DeviceConfig analytics trace: ".concat(e6));
                }, e5.prototype.onAssignment = function(e6) {
                  this.enableConsoleLog && console.log("DeviceConfig analytics assignment: ".concat(e6));
                }, e5;
              })();
              t3.DefaultAnalytics = a;
              var s = (function() {
                function e5(e6, t4) {
                  this.prefix = e6, this.storage = t4;
                }
                return e5.prototype.getJson = function(e6) {
                  var t4 = this.prefix + "_" + e6;
                  try {
                    var r4 = this.storage.getItem(t4);
                    if (r4) return JSON.parse(r4);
                  } catch (e7) {
                    console.error("Error getting local storage key ".concat(t4) + e7);
                  }
                  return null;
                }, e5.prototype.setJson = function(e6, t4) {
                  var r4 = this.prefix + "_" + e6;
                  try {
                    this.storage.setItem(r4, JSON.stringify(t4));
                  } catch (e7) {
                    console.error("Error setting local storage key ".concat(r4, ": ") + e7);
                  }
                }, e5;
              })();
              t3.DefaultStorage = s;
              var u = (function() {
                function e5() {
                }
                return e5.prototype.getMillis = function() {
                  return Date.now();
                }, e5;
              })();
              t3.DefaultClock = u;
            }, 9225: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.BucketAssignmentManager = t3.PERCENTAGE_SUM_TOLERANCE = void 0;
              var n2 = r3(8177), i = r3(4293), o = r3(5234);
              t3.PERCENTAGE_SUM_TOLERANCE = 1e-3;
              var a = (function() {
                function e5() {
                }
                return e5.prototype.roll = function(e6) {
                  var t4 = (0, n2.getSecureRandom)();
                  return t4.method !== n2.SecureRandomMethod.CRYPTO_RANDOM_VALUES && e6.push({ type: o.RolloutWarningType.NON_IDEAL_RANDOM_GENERATED, context: { details: t4.method } }), t4.value;
                }, e5.prototype.assignVariant = function(e6, t4) {
                  var r4 = [], n3 = this.validateRollout(e6, r4);
                  return n3.ok ? t4 && e6["rollout-id"] === t4.rolloutId ? (0, i.ok)({ variant: t4.assignedVariant, shouldUpdateState: false, warnings: r4 }) : e6["force-redistribute"] ? this.performInitialAssignment(e6, r4) : t4 ? this.performStickyAssignment(e6, t4, r4) : this.performInitialAssignment(e6, r4) : n3;
                }, e5.prototype.validateRollout = function(e6, r4) {
                  var n3 = e6.variants;
                  if (0 === n3.length) return (0, i.err)("Empty variants array");
                  var o2 = this.calculateTotalPercentage(n3, r4);
                  return Math.abs(o2 - 1) > t3.PERCENTAGE_SUM_TOLERANCE ? (0, i.err)("Variant percentages don't sum to 1.0 within ".concat(t3.PERCENTAGE_SUM_TOLERANCE, " tolerance (got ").concat(o2, ")")) : (0, i.ok)(e6);
                }, e5.prototype.calculateTotalPercentage = function(e6, t4) {
                  return e6.reduce(function(e7, r4) {
                    var n3 = Object.values(r4);
                    return n3.length > 1 && t4.push({ type: o.RolloutWarningType.UNEXPECTED_VARIANT_PROPERTIES, context: { details: "Found more than 1 value for variant: ".concat(n3) } }), e7 + n3[0];
                  }, 0);
                }, e5.prototype.performInitialAssignment = function(e6, t4) {
                  var r4 = this.roll(t4), n3 = this.selectVariantFromRoll(e6.variants, r4);
                  return (0, i.ok)({ variant: n3, shouldUpdateState: true, warnings: t4 });
                }, e5.prototype.performStickyAssignment = function(e6, t4, r4) {
                  var n3 = t4.assignedVariant, a2 = t4.rolloutConfig, s = this.getVariantPercentage(e6.variants, n3), u = this.getVariantPercentage(a2.variants, n3);
                  if (!s.ok) return this.performInitialAssignment(e6, r4);
                  var c = s.value;
                  if (!u.ok || c >= u.value) return u.ok || r4.push({ type: o.RolloutWarningType.PREVIOUS_VARIANT_NOT_FOUND, context: { details: "Previous variant not found: ".concat(n3) } }), (0, i.ok)({ variant: n3, shouldUpdateState: true, warnings: r4 });
                  var l = c / u.value;
                  if (this.roll(r4) <= l) return (0, i.ok)({ variant: n3, shouldUpdateState: true, warnings: r4 });
                  var d = this.calculateFreeCapacity(e6.variants, a2.variants), f = this.roll(r4), h = this.selectVariantFromRoll(d, f);
                  return (0, i.ok)({ variant: h, shouldUpdateState: true, warnings: r4 });
                }, e5.prototype.getVariantPercentage = function(e6, t4) {
                  for (var r4 = 0, n3 = e6; r4 < n3.length; r4++) {
                    var o2 = n3[r4];
                    if (void 0 !== o2[t4]) return (0, i.ok)(o2[t4]);
                  }
                  return (0, i.err)("variant not found");
                }, e5.prototype.selectVariantFromRoll = function(e6, t4) {
                  for (var r4 = 0, n3 = 0, i2 = e6; n3 < i2.length; n3++) {
                    var o2 = i2[n3], a2 = Object.entries(o2)[0], s = a2[0];
                    if (t4 <= (r4 += a2[1])) return s;
                  }
                  return Object.keys(e6[e6.length - 1])[0];
                }, e5.prototype.calculateFreeCapacity = function(e6, t4) {
                  for (var r4, n3 = [], i2 = 0, o2 = e6; i2 < o2.length; i2++) {
                    var a2 = o2[i2], s = Object.entries(a2)[0], u = s[0], c = s[1], l = this.getVariantPercentage(t4, u), d = 0;
                    l.ok && (d = l.value), c > d && n3.push(((r4 = {})[u] = c - d, r4));
                  }
                  return n3;
                }, e5;
              })();
              t3.BucketAssignmentManager = a;
            }, 5234: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.RolloutManager = t3.RolloutWarningType = void 0;
              var n2, i = r3(2570), o = r3(9225), a = r3(8177), s = r3(4293);
              !(function(e5) {
                e5.STORAGE_INITIAL_LOAD_FAILED = "STORAGE_INITIAL_LOAD_FAILED", e5.STORAGE_INITIAL_LOAD_VERSION_MISMATCH = "STORAGE_INITIAL_LOAD_VERSION_MISMATCH", e5.STORAGE_INITIAL_SAVE_FAILED = "STORAGE_INITIAL_SAVE_FAILED", e5.STORAGE_SAVE_FAILED = "STORAGE_SAVE_FAILED", e5.UNEXPECTED_VARIANT_PROPERTIES = "UNEXPECTED_VARIANT_PROPERTIES", e5.PREVIOUS_VARIANT_NOT_FOUND = "PREVIOUS_VARIANT_NOT_FOUND", e5.NON_IDEAL_UUID_GENERATED = "NON_IDEAL_UUID_GENERATED", e5.NON_IDEAL_RANDOM_GENERATED = "NON_IDEAL_RANDOM_GENERATED";
              })(n2 || (t3.RolloutWarningType = n2 = {}));
              var u = (function() {
                function e5(e6) {
                  this.rolloutStateManager = new i.LocalStorageRolloutStateManager(e6), this.bucketAssignmentManager = new o.BucketAssignmentManager();
                }
                return e5.prototype.getVariantAssignment = function(e6) {
                  var t4 = [], r4 = e6["feature-id"], i2 = this.rolloutStateManager.getRolloutState(r4), o2 = i2.state, u2 = i2.warnings;
                  t4.push.apply(t4, u2);
                  var c = this.bucketAssignmentManager.assignVariant(e6, o2);
                  if (!c.ok) return (0, s.err)(c.error);
                  t4.push.apply(t4, c.value.warnings);
                  var l = null == o2 ? void 0 : o2.trackingId;
                  if (!l) {
                    var d = (0, a.generateUuid)(), f = d.value, h = d.method;
                    l = f, h !== a.UuidMethod.CRYPTO_RANDOM_UUID && t4.push({ type: n2.NON_IDEAL_UUID_GENERATED, context: { details: h } });
                  }
                  if (c.value.shouldUpdateState) {
                    var p = { rolloutId: e6["rollout-id"], assignedVariant: c.value.variant, rolloutConfig: e6, trackingId: l }, v = this.rolloutStateManager.setRolloutState(r4, p);
                    t4.push.apply(t4, v.warnings);
                  }
                  return (0, s.ok)({ variant: c.value.variant, featureId: r4, rolloutId: e6["rollout-id"], trackingId: l, warnings: t4 });
                }, e5;
              })();
              t3.RolloutManager = u;
            }, 7974: function(e4, t3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.DeviceConfigRolloutSchema = t3.DeviceConfigRolloutVariantSchema = void 0, t3.DeviceConfigRolloutVariantSchema = {}, t3.DeviceConfigRolloutSchema = { variants: { type: "array", required: true, items: { type: "object", required: true, config: { schema: t3.DeviceConfigRolloutVariantSchema } } }, "feature-id": { type: "string", required: true }, "rollout-id": { type: "number", required: true }, "force-redistribute": { type: "boolean", required: false }, version: { type: "number", required: true } };
            }, 2570: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.LocalStorageRolloutStateManager = t3.DEVICE_CONFIG_STORAGE_VERSION = void 0;
              var i = r3(5234);
              t3.DEVICE_CONFIG_STORAGE_VERSION = 1;
              var o = (function() {
                function e5(e6) {
                  this.storageKey = (function(e7) {
                    return "_amazon_ivs_dc_".concat(e7, "_b8e4f7c1");
                  })(e6);
                  var t4 = this.loadStorage(), r4 = t4.storage, n3 = t4.warnings;
                  this.storage = r4, this.loadWarnings = n3;
                }
                return e5.prototype.getRolloutState = function(e6) {
                  var t4;
                  return { state: (null === (t4 = this.storage.features[e6]) || void 0 === t4 ? void 0 : t4.rolloutState) || null, warnings: n2([], this.loadWarnings, true) };
                }, e5.prototype.setRolloutState = function(e6, t4) {
                  return this.storage.features[e6] || (this.storage.features[e6] = {}), this.storage.features[e6].rolloutState = t4, this.saveStorage();
                }, e5.prototype.loadStorage = function() {
                  var e6 = [];
                  try {
                    var r4 = localStorage.getItem(this.storageKey);
                    if (r4) {
                      var n3 = JSON.parse(r4);
                      if ("object" == typeof n3) {
                        if (n3.version === t3.DEVICE_CONFIG_STORAGE_VERSION) return { storage: n3, warnings: e6 };
                        e6.push({ type: i.RolloutWarningType.STORAGE_INITIAL_LOAD_VERSION_MISMATCH, context: { details: "Expected version ".concat(t3.DEVICE_CONFIG_STORAGE_VERSION, ", but got ").concat(n3.version) } });
                      }
                    }
                  } catch (t4) {
                    e6.push({ type: i.RolloutWarningType.STORAGE_INITIAL_LOAD_FAILED, context: { error: t4 } });
                  }
                  var o2 = { version: t3.DEVICE_CONFIG_STORAGE_VERSION, features: {} };
                  try {
                    localStorage.setItem(this.storageKey, JSON.stringify(o2));
                  } catch (t4) {
                    e6.push({ type: i.RolloutWarningType.STORAGE_INITIAL_SAVE_FAILED, context: { error: t4 } });
                  }
                  return { storage: o2, warnings: e6 };
                }, e5.prototype.saveStorage = function() {
                  try {
                    return localStorage.setItem(this.storageKey, JSON.stringify(this.storage)), { warnings: [] };
                  } catch (e6) {
                    return { warnings: [{ type: i.RolloutWarningType.STORAGE_SAVE_FAILED, context: { error: e6 } }] };
                  }
                }, e5;
              })();
              t3.LocalStorageRolloutStateManager = o;
            }, 3259: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.daysToSeconds = t3.hoursToSeconds = t3.minutesToSeconds = t3.secondsToMillis = t3.createPropertyMap = void 0;
              var n2 = r3(4811);
              t3.createPropertyMap = function(e5) {
                return new Map(Object.entries(e5).map(function(e6) {
                  var t4 = e6[0], r4 = e6[1];
                  return [t4, new n2.Property(t4, "string" == typeof r4 ? n2.ValueType.STRING : "number" == typeof r4 ? n2.ValueType.NUMBER : "boolean" == typeof r4 ? n2.ValueType.BOOLEAN : n2.ValueType.JSON, "string" == typeof r4 ? r4 : void 0, "number" == typeof r4 ? r4 : void 0, "boolean" == typeof r4 ? r4 : void 0, "object" == typeof r4 ? JSON.stringify(r4) : void 0, t4)];
                }));
              }, t3.secondsToMillis = function(e5) {
                return 1e3 * e5;
              }, t3.minutesToSeconds = function(e5) {
                return 60 * e5;
              }, t3.hoursToSeconds = function(e5) {
                return 60 * e5 * 60;
              }, t3.daysToSeconds = function(e5) {
                return 60 * e5 * 60 * 24;
              };
            }, 3723: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.WindowPropertyOverrider = void 0;
              var n2 = r3(3259);
              t3.WindowPropertyOverrider = function(e5) {
                return { getPropertyOverride: function(t4) {
                  var r4, i, o;
                  if (true === (null === (i = null == e5 ? void 0 : e5.AMAZON_IVS_DEVICE_CONFIG) || void 0 === i ? void 0 : i.enableOverrides) && void 0 !== (null === (o = e5.AMAZON_IVS_DEVICE_CONFIG.propertyOverrides) || void 0 === o ? void 0 : o[t4])) {
                    var a = e5.AMAZON_IVS_DEVICE_CONFIG.propertyOverrides;
                    return (0, n2.createPropertyMap)((r4 = {}, r4[t4] = a[t4], r4)).get(t4);
                  }
                } };
              };
            }, 1743: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__setModuleDefault || (Object.create ? function(e5, t4) {
                Object.defineProperty(e5, "default", { enumerable: true, value: t4 });
              } : function(e5, t4) {
                e5.default = t4;
              }), o = this && this.__importStar || function(e5) {
                if (e5 && e5.__esModule) return e5;
                var t4 = {};
                if (null != e5) for (var r4 in e5) "default" !== r4 && Object.prototype.hasOwnProperty.call(e5, r4) && n2(t4, e5, r4);
                return i(t4, e5), t4;
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.validateSchemaAndReturn = t3.validateSchema = void 0;
              var a = o(r3(4293));
              function s(e5, t4) {
                var r4;
                if (null == e5) return a.err("value is undefined or null");
                if ("object" != typeof e5) return a.err("value is not an object");
                for (var n3 = e5, i2 = 0, o2 = Object.entries(t4); i2 < o2.length; i2++) {
                  var u = o2[i2], c = u[0], l = u[1];
                  if (l.required || !(c in n3) || void 0 !== n3[c]) if ("object" === l.type) {
                    if (l.required && !(c in n3)) return a.err("".concat(c, " is required in schema"));
                    if (n3[c] && !(p = s(n3[c], null === (r4 = null == l ? void 0 : l.config) || void 0 === r4 ? void 0 : r4.schema)).ok) return a.err("".concat(c, " ").concat(p.error));
                  } else if ("array" === l.type) {
                    if (l.required && !(c in n3)) return a.err("".concat(c, " is required in schema"));
                    if (c in n3) {
                      if (!Array.isArray(n3[c])) return a.err("".concat(c, " is not an array"));
                      if (l.items) for (var d = 0, f = n3[c]; d < f.length; d++) {
                        var h = f[d];
                        if ("string" === l.items.type || "number" === l.items.type || "boolean" === l.items.type) {
                          if (typeof h !== l.items.type) return a.err("".concat(h, " in ").concat(c, " is not an ").concat(l.items.type));
                        } else if ("object" === l.items.type && l.items.config) {
                          var p;
                          if (!(p = s(h, l.items.config.schema)).ok) return p;
                        }
                      }
                    }
                  } else {
                    if (l.required && !(c in n3)) return a.err("".concat(c, " is required in schema"));
                    if (c in n3 && typeof n3[c] !== l.type) return a.err("".concat(c, " is not of type ").concat(l.type));
                  }
                }
                return a.ok(true);
              }
              t3.validateSchema = s, t3.validateSchemaAndReturn = function(e5, t4) {
                var r4 = s(e5, t4);
                return r4.ok ? a.ok(e5) : a.err(r4.error);
              };
            }, 3840: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.validateSchema = void 0;
              var n2 = r3(1743);
              Object.defineProperty(t3, "validateSchema", { enumerable: true, get: function() {
                return n2.validateSchema;
              } });
            }, 4811: function(e4, t3, r3) {
              "use strict";
              var n2, i = this && this.__extends || (n2 = function(e5, t4) {
                return n2 = Object.setPrototypeOf || { __proto__: [] } instanceof Array && function(e6, t5) {
                  e6.__proto__ = t5;
                } || function(e6, t5) {
                  for (var r4 in t5) Object.prototype.hasOwnProperty.call(t5, r4) && (e6[r4] = t5[r4]);
                }, n2(e5, t4);
              }, function(e5, t4) {
                if ("function" != typeof t4 && null !== t4) throw new TypeError("Class extends value " + String(t4) + " is not a constructor or null");
                function r4() {
                  this.constructor = e5;
                }
                n2(e5, t4), e5.prototype = null === t4 ? Object.create(t4) : (r4.prototype = t4.prototype, new r4());
              });
              Object.defineProperty(t3, "__esModule", { value: true }), t3.HttpError = t3.StateHolder = t3.Property = t3.ValueType = t3.DeviceConfigEnv = void 0;
              var o, a, s = r3(7094);
              !(function(e5) {
                e5.BETA = "beta", e5.PROD = "prod", e5.CUSTOM = "custom";
              })(o || (t3.DeviceConfigEnv = o = {})), (function(e5) {
                e5[e5.STRING = 0] = "STRING", e5[e5.NUMBER = 1] = "NUMBER", e5[e5.BOOLEAN = 2] = "BOOLEAN", e5[e5.JSON = 3] = "JSON";
              })(a || (t3.ValueType = a = {})), t3.Property = function(e5, t4, r4, n3, i2, o2, u2, c2) {
                switch (this.name = e5, this.type = t4, this.valueString = r4, this.valueNumber = n3, this.valueBoolean = i2, this.valueJson = o2, this.valueAnalytics = u2, this.type) {
                  case a.STRING:
                    c2 === s.ENCODING_BASE64 && r4 && (this.valueString = atob(r4));
                    break;
                  case a.JSON:
                    c2 === s.ENCODING_BASE64 && o2 && (this.valueJson = atob(o2));
                }
              };
              var u = (function() {
                function e5(e6, t4) {
                  var r4;
                  this.storage = e6, this.key = t4, this.state = null !== (r4 = e6.getJson(t4)) && void 0 !== r4 ? r4 : {};
                }
                return e5.prototype.getState = function() {
                  return this.state;
                }, e5.prototype.setState = function(e6) {
                  Object.assign(this.state, e6), this.storage.setJson(this.key, this.state);
                }, e5;
              })();
              t3.StateHolder = u;
              var c = (function(e5) {
                function t4(t5) {
                  return e5.call(this, t5) || this;
                }
                return i(t4, e5), t4;
              })(Error);
              t3.HttpError = c;
            }, 363: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__assign || function() {
                return n2 = Object.assign || function(e5) {
                  for (var t4, r4 = 1, n3 = arguments.length; r4 < n3; r4++) for (var i2 in t4 = arguments[r4]) Object.prototype.hasOwnProperty.call(t4, i2) && (e5[i2] = t4[i2]);
                  return e5;
                }, n2.apply(this, arguments);
              }, i = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.DeviceConfigPropertyHolder = t3.DeviceConfigManager = void 0;
              var o = r3(4478), a = r3(6270), s = r3(4355), u = r3(7094), c = r3(3259), l = r3(1743), d = r3(3723), f = r3(5234), h = r3(7974), p = r3(4811), v = (function() {
                function e5(e6, t4) {
                  var r4, i2, o2, a2;
                  if (!t4.fileKey) throw new Error("options.fileKey cannot be empty");
                  var s2 = null !== (r4 = t4.clock) && void 0 !== r4 ? r4 : new u.DefaultClock(), c2 = s2.getMillis();
                  switch (this.context = e6, this.fileKey = t4.fileKey, this.env = t4.standardEnv, t4.standardEnv) {
                    case p.DeviceConfigEnv.BETA:
                      this.fetchServer = "beta.ivs-device-config-beta.live-video.net";
                      break;
                    case p.DeviceConfigEnv.PROD:
                      this.fetchServer = "prod.ivs-device-config.live-video.net";
                      break;
                    case p.DeviceConfigEnv.CUSTOM:
                      if (!t4.customServer) throw new Error("Custom env requires options.customServer");
                      this.fetchServer = t4.customServer;
                      break;
                    default:
                      throw new Error("Invalid value for standardEnv: ".concat(t4.standardEnv));
                  }
                  this.trace = null !== (i2 = t4.trace) && void 0 !== i2 ? i2 : new u.DefaultTrace(t4.enableConsoleLog), this.fetch = null !== (o2 = t4.fetch) && void 0 !== o2 ? o2 : new m(e6), this.storage = new u.DefaultStorage(u.STORAGE_PREFIX_KEY + t4.fileKey, e6.localStorage), this.state = new p.StateHolder(this.storage, u.STATE_KEY), this.refresh = n2(n2({}, (0, u.getDefaultConfigRefreshOptions)()), t4.refresh), this.emitMetrics = t4.emitMetrics, this.analyticsProperties = null !== (a2 = t4.analyticsProperties) && void 0 !== a2 ? a2 : (0, u.getDefaultCommonAnalytics)(), this.enableConsoleLog = t4.enableConsoleLog, this.clock = s2, this.isInitialRefreshDone = false;
                  var l2 = 0, d2 = this.state.getState();
                  d2 && d2.fetchServer === this.fetchServer && d2.lastFetchWhenMs && (l2 = d2.lastFetchWhenMs);
                  var f2, h2 = s2.getMillis(), v2 = (h2 - l2) / 1e3;
                  if (v2 <= this.refresh.maxCacheAgeSeconds) {
                    var g2 = this.storage.getJson(u.DATA_KEY);
                    g2 && g2.fetchServer === this.fetchServer && this.setData(g2.json);
                  }
                  this.lastUseMs = h2, v2 >= this.refresh.refreshIntervalSeconds || 0 == l2 ? f2 = 0 : (f2 = Math.round(this.refresh.refreshIntervalSeconds - v2), this.isInitialRefreshDone = true), f2 < 5 && (f2 = u.DEFAULT_INITIAL_DELAY_SECONDS), this.trace.onTrace("Will start refresh in " + f2 + " seconds"), this.fetchTask = this.context.setTimeout(this.startRefresh.bind(this), 1e3 * f2);
                  var y = s2.getMillis();
                  this.initialLoadTime = y - c2;
                }
                return e5.getInstance = function(t4, r4) {
                  try {
                    if (!(t4.fetch && t4.setTimeout && t4.clearTimeout && t4.localStorage)) return void console.log("Context needs to provide fetch, setTimeout, clearTimeout, localStorage");
                  } catch (e6) {
                    return void console.error("Error checking for context properties " + e6);
                  }
                  if (this.gInstance) {
                    if (this.gInstance.fileKey != r4.fileKey) throw new Error("Existing instance has file key ".concat(this.gInstance.fileKey, ", now asking for ").concat(r4.fileKey));
                    return this.gInstance;
                  }
                  return this.gInstance = new e5(t4, r4), this.gInstance;
                }, e5.prototype.getConfigurationHolder = function(e6) {
                  var t4, r4, n3, i2 = this.clock.getMillis();
                  this.lastUseMs = i2;
                  var o2 = null !== (t4 = e6.analytics) && void 0 !== t4 ? t4 : new u.DefaultAnalytics(this.enableConsoleLog), a2 = 0, s2 = this.state.getState();
                  return s2 && s2.fetchServer === this.fetchServer && s2.lastFetchWhenMs && (a2 = s2.lastFetchWhenMs), (i2 - a2) / 1e3 <= this.refresh.maxCacheAgeSeconds ? new g(o2, this.analyticsProperties, this.properties, a2, this.clock, this.fileKey, null !== (r4 = this.enableConsoleLog) && void 0 !== r4 && r4) : new g(o2, this.analyticsProperties, void 0, 0, this.clock, this.fileKey, null !== (n3 = this.enableConsoleLog) && void 0 !== n3 && n3);
                }, e5.prototype.getFetchUrl = function() {
                  var e6 = "https://".concat(this.fetchServer, "/").concat(this.fileKey, ".json"), t4 = new URL(e6);
                  return t4.searchParams.set("version", "1.0"), t4;
                }, e5.clearInstance = function() {
                  var e6;
                  null === (e6 = this.gInstance) || void 0 === e6 || e6.clearInstanceImpl(), this.gInstance = void 0;
                }, e5.prototype.clearInstanceImpl = function() {
                  this.context.clearTimeout(this.fetchTask), this.fetchTask = void 0, this.context.clearTimeout(this.retryTask), this.retryTask = void 0;
                }, e5.prototype.startRefresh = function() {
                  var e6 = this;
                  this.trace.onTrace("Starting refresh request"), this.fetchTask = void 0, this.retryTask && (this.context.clearTimeout(this.retryTask), this.retryTask = void 0), this.fetchTask = this.context.setTimeout(this.startRefresh.bind(this), 1e3 * this.refresh.refreshIntervalSeconds);
                  var t4 = this.clock.getMillis();
                  if ((t4 - this.lastUseMs) / 1e3 > this.refresh.stopRefreshAfterSeconds) this.trace.onTrace("Will not refresh due to refresh timeout");
                  else if (!this.isInitialRefreshDone || this.refresh.canRefreshNow()) {
                    this.isInitialRefreshDone = true;
                    var r4 = {};
                    this.fetchData().then(function(n3) {
                      e6.processResponse(n3, r4, t4), e6.emitMetricsImpl(r4);
                    }).catch(function(t5) {
                      e6.exceptionMetrics(r4, t5), e6.emitMetricsImpl(r4), e6.trace.onTrace("Fetch error: " + t5), e6.refresh.retryCount > 0 && e6.scheduleRetry(1);
                    });
                  } else this.trace.onTrace("Will not refresh due to callback having returned false");
                }, e5.prototype.fetchData = function() {
                  var e6 = this.getFetchUrl(), t4 = new Headers(), r4 = this.state.getState();
                  return r4.fetchServer === this.fetchServer && (r4.lastFetchWhenFullMs && this.clock.getMillis() - r4.lastFetchWhenFullMs > 1e3 * (0, c.hoursToSeconds)(24) ? this.trace.onTrace("Forcing full refresh") : this.properties && r4.lastFetchEtagHeader && t4.set("If-None-Match", r4.lastFetchEtagHeader)), this.fetch.fetchUrl(e6, t4);
                }, e5.prototype.processResponse = function(e6, t4, r4) {
                  var n3 = { fetchServer: this.fetchServer, lastFetchWhenMs: this.clock.getMillis() }, i2 = e6.headers.get("etag");
                  if (i2 && (n3.lastFetchEtagHeader = i2), e6.status < 300 && e6.json) if (this.setData(e6.json)) {
                    this.trace.onTrace("Successfully parsed fetched data"), this.storage.setJson(u.DATA_KEY, { fetchServer: this.fetchServer, json: e6.json });
                    var o2 = this.clock.getMillis();
                    n3.lastFetchWhenFullMs = o2, this.state.setState(n3), t4.success_new_data_count = 1, t4.fetch_duration_average = Math.max(0, o2 - r4);
                  } else t4.fail_invalid_data_count = 1;
                  else {
                    if (304 !== e6.status) {
                      var a2 = "Unexpected http fetch status: " + e6.status;
                      throw this.trace.onTrace(a2), new p.HttpError(a2);
                    }
                    this.trace.onTrace("Server said no change in data"), o2 = this.clock.getMillis(), n3.lastFetchWhenMs = o2, this.state.setState(n3), t4.success_no_change_count = 1, t4.fetch_duration_average = Math.max(0, o2 - r4);
                  }
                }, e5.prototype.scheduleRetry = function(e6) {
                  var t4 = this;
                  this.retryTask && this.context.clearTimeout(this.retryTask);
                  var r4 = e6 * this.refresh.retryIntervalSeconds;
                  this.retryTask = this.context.setTimeout(function() {
                    t4.retryTask = void 0, t4.fetchRetry(e6);
                  }, 1e3 * r4);
                }, e5.prototype.fetchRetry = function(e6) {
                  var t4 = this;
                  this.trace.onTrace("Starting retry request ".concat(e6, "..."));
                  var r4 = {}, n3 = this.clock.getMillis();
                  this.fetchData().then(function(e7) {
                    t4.processResponse(e7, r4, n3), t4.emitMetricsImpl(r4);
                  }).catch(function(n4) {
                    t4.exceptionMetrics(r4, n4), t4.emitMetricsImpl(r4), t4.trace.onTrace("Fetch error: " + n4), t4.refresh.retryCount > e6 && t4.scheduleRetry(e6 + 1);
                  });
                }, e5.prototype.setData = function(e6) {
                  if (!e6) return false;
                  if ("1.0" !== e6.version) return this.trace.onTrace("Data version is not 1.0, not applying"), false;
                  var t4 = e6.properties;
                  if (!t4 || !Array.isArray(t4)) return this.trace.onTrace("Data properties is not an array, not applying"), false;
                  for (var r4 = /* @__PURE__ */ new Map(), n3 = 0, i2 = t4; n3 < i2.length; n3++) {
                    var o2 = i2[n3];
                    if (o2.name && o2.type && this.isValidValueType(o2.type)) {
                      if (o2.value_string && "string" != typeof o2.value_string) {
                        this.trace.onTrace("Invalid type of value.value_string " + typeof o2.value_string);
                        continue;
                      }
                      if (o2.value_number && "number" != typeof o2.value_number) {
                        this.trace.onTrace("Invalid type of value.value_number " + typeof o2.value_number);
                        continue;
                      }
                      if (o2.value_boolean && "boolean" != typeof o2.value_boolean) {
                        this.trace.onTrace("Invalid type of value.value_boolean " + typeof o2.value_boolean);
                        continue;
                      }
                      if (o2.value_json && "string" != typeof o2.value_json) {
                        this.trace.onTrace("Invalid type of value.value_json " + typeof o2.value_json);
                        continue;
                      }
                      if (o2.value_analytics && "string" != typeof o2.value_analytics) {
                        this.trace.onTrace("Invalid type of item.value_analytics " + typeof o2.value_analytics);
                        continue;
                      }
                      var a2 = new p.Property(o2.name, this.parseValueType(o2.type), o2.value_string, o2.value_number, o2.value_boolean, o2.value_json, o2.value_analytics, o2.encoding);
                      r4.set(a2.name, a2);
                    }
                  }
                  return 0 != r4.size && (this.properties = r4, true);
                }, e5.prototype.isValidValueType = function(e6) {
                  return "string" === e6 || "number" === e6 || "boolean" === e6 || "json" == e6;
                }, e5.prototype.parseValueType = function(e6) {
                  switch (e6) {
                    case "string":
                      return p.ValueType.STRING;
                    case "number":
                      return p.ValueType.NUMBER;
                    case "boolean":
                      return p.ValueType.BOOLEAN;
                    case "json":
                      return p.ValueType.JSON;
                    default:
                      throw new Error("Unsupported value type ".concat(e6));
                  }
                }, e5.prototype.emitMetricsImpl = function(e6) {
                  var t4, r4, i2, o2, a2, s2, u2 = n2(n2({}, this.analyticsProperties), { initial_load_time: this.initialLoadTime, fetch_attempt_count: 1, fetch_duration_average: null !== (t4 = e6.fetch_duration_average) && void 0 !== t4 ? t4 : 0, success_no_change_count: null !== (r4 = e6.success_no_change_count) && void 0 !== r4 ? r4 : 0, success_new_data_count: null !== (i2 = e6.success_new_data_count) && void 0 !== i2 ? i2 : 0, fail_exception_count: null !== (o2 = e6.fail_exception_count) && void 0 !== o2 ? o2 : 0, fail_http_error_count: null !== (a2 = e6.fail_http_error_count) && void 0 !== a2 ? a2 : 0, fail_invalid_data_count: null !== (s2 = e6.fail_invalid_data_count) && void 0 !== s2 ? s2 : 0 });
                  this.emitMetrics(u2);
                }, e5.prototype.exceptionMetrics = function(e6, t4) {
                  t4 instanceof p.HttpError || "string" == typeof (null == t4 ? void 0 : t4.message) && 0 === t4.message.indexOf("http error") ? e6.fail_http_error_count = 1 : e6.fail_exception_count = 1;
                }, e5;
              })();
              t3.DeviceConfigManager = v;
              var g = (function() {
                function e5(e6, t4, r4, i2, o2, a2, s2) {
                  void 0 === s2 && (s2 = false);
                  var u2 = this;
                  this.emitValue = function(e7, t5) {
                    u2.analytics.onValue(n2(n2({}, u2.analyticsProperties), { key_name: e7, value: "".concat(t5), fetched_seconds_ago: u2.getFetchedSecondsAgo() }));
                  }, this.emitTrace = function(e7, t5) {
                    u2.analytics.onTrace(n2(n2({}, u2.analyticsProperties), { key_name: e7, message: t5 }));
                  }, this.emitError = function(e7, t5) {
                    u2.analytics.onError(n2(n2({}, u2.analyticsProperties), { key_name: e7, message: t5 }));
                  }, this.emitAssignment = function(e7) {
                    var t5 = e7.warnings.map(function(e8) {
                      return e8.type;
                    }).join(",");
                    u2.analytics.onAssignment(n2(n2({}, u2.analyticsProperties), { assignment: e7.variant, feature_id: e7.featureId, rollout_id: e7.rolloutId, tracking_id: e7.trackingId, warnings: t5 }));
                  }, this.analytics = e6, this.analyticsProperties = t4, this.properties = r4, this.dataFetchedWhenMs = i2, this.clock = o2, this.fileKey = a2, this.enableConsoleLog = s2, this.overrider = (0, d.WindowPropertyOverrider)(window);
                }
                return e5.prototype.getSize = function() {
                  return this.properties ? this.properties.size : -1;
                }, e5.prototype.getProperty = function(e6) {
                  var t4 = this.overrider.getPropertyOverride(e6);
                  if (t4) return this.debugLog("Using override for key: ".concat(e6)), t4;
                  if (this.properties) {
                    var r4 = this.properties.get(e6);
                    if (r4) return r4;
                    this.emitError(e6, "No property exists for key name");
                  } else this.emitTrace(e6, "No properties, local cache is expired");
                }, e5.prototype.getStringValue = function(e6) {
                  var t4, r4, n3 = this.getProperty(e6);
                  if (n3) {
                    if (n3.type === p.ValueType.STRING) {
                      var i2 = null !== (t4 = n3.valueString) && void 0 !== t4 ? t4 : "";
                      return this.emitValue(n3.name, null !== (r4 = n3.valueAnalytics) && void 0 !== r4 ? r4 : i2), i2;
                    }
                    this.emitError(n3.name, "Type is not a string");
                  }
                }, e5.prototype.getNumberValue = function(e6) {
                  var t4, r4, n3 = this.getProperty(e6);
                  if (n3) {
                    if (n3.type === p.ValueType.NUMBER) {
                      var i2 = null !== (t4 = n3.valueNumber) && void 0 !== t4 ? t4 : 0;
                      return this.emitValue(n3.name, null !== (r4 = n3.valueAnalytics) && void 0 !== r4 ? r4 : i2), i2;
                    }
                    this.emitError(n3.name, "Type is not a number");
                  }
                }, e5.prototype.getBooleanValue = function(e6) {
                  var t4, r4, n3 = this.getProperty(e6);
                  if (n3) {
                    if (n3.type === p.ValueType.BOOLEAN) {
                      var i2 = null !== (t4 = n3.valueBoolean) && void 0 !== t4 && t4;
                      return this.emitValue(n3.name, null !== (r4 = n3.valueAnalytics) && void 0 !== r4 ? r4 : i2), i2;
                    }
                    this.emitError(n3.name, "Type is not a boolean");
                  }
                }, e5.prototype.getJsonValue = function(e6) {
                  var t4, r4, n3 = this.getProperty(e6);
                  if (n3) if (n3.type === p.ValueType.JSON) {
                    var i2 = null !== (t4 = n3.valueAnalytics) && void 0 !== t4 ? t4 : "", o2 = n3.valueJson;
                    if (o2) try {
                      var a2 = JSON.parse(o2);
                      return this.emitValue(n3.name, null !== (r4 = n3.valueAnalytics) && void 0 !== r4 ? r4 : a2), a2;
                    } catch (e7) {
                      return void this.emitError(n3.name, "JSON parse error");
                    }
                    else this.emitValue(n3.name, i2);
                  } else this.emitError(n3.name, "Type is not JSON");
                }, e5.prototype.getResolvedConfigForPropertyKey = function(e6) {
                  var t4 = e6.key, r4 = e6.context, n3 = e6.schema, u2 = e6.trace, c2 = void 0 !== u2 && u2, d2 = this.getJsonValue(t4);
                  if (void 0 !== d2) if (0 !== d2.length) {
                    for (var f2 = [], h2 = 0, p2 = d2; h2 < p2.length; h2++) {
                      var v2 = p2[h2];
                      (_ = (0, l.validateSchema)(v2, a.DeviceConfigCriteriaConfigSchema)).ok ? f2.push(v2) : this.emitError(t4, "Criteria structure invalid: ".concat(_.error));
                    }
                    for (var g2 = new s.CriteriaInputs(r4), m2 = [], y = 0, b = f2; y < b.length; y++) v2 = b[y], (_ = a.CriteriaParser.evaluateCriteria(v2, g2)).ok ? _.value.matched && m2.push(_.value.payload) : this.emitError(t4, _.error);
                    if (0 !== m2.length) {
                      for (var E = [], S = 0, T = m2; S < T.length; S++) {
                        var _;
                        v2 = T[S], (_ = (0, l.validateSchema)(v2, n3)).ok ? E.push(v2) : this.emitError(t4, "Payload structure invalid: ".concat(_.error));
                      }
                      var C = o.mergeConfigs.apply(void 0, i([{}], E, false)), k = JSON.stringify(C);
                      return this.emitValue(t4, "Resolved: ".concat(k)), C;
                    }
                    if (c2) {
                      var w = f2.length;
                      this.emitTrace(t4, "No matches from ".concat(w, " payloads"));
                    }
                  } else c2 && this.emitTrace(t4, "No payloads exist for key");
                }, e5.prototype.getResolvedExperimentForPropertyKeys = function(e6) {
                  for (var t4 = e6.keys, r4 = e6.context, n3 = e6.trace, i2 = void 0 !== n3 && n3, o2 = {}, a2 = new f.RolloutManager(this.fileKey), s2 = 0, u2 = t4; s2 < u2.length; s2++) {
                    var c2 = u2[s2];
                    o2[c2] = this.processExperimentKey(c2, r4, i2, a2);
                  }
                  return o2;
                }, e5.prototype.processExperimentKey = function(e6, t4, r4, n3) {
                  var i2 = this.getJsonValue(e6);
                  if (void 0 !== i2) if (0 !== i2.length) {
                    for (var o2 = [], s2 = 0, u2 = i2; s2 < u2.length; s2++) {
                      var c2 = u2[s2], d2 = (0, l.validateSchema)(c2, a.DeviceConfigCriteriaConfigSchema);
                      d2.ok ? o2.push(c2) : this.emitError(e6, "Criteria structure invalid: ".concat(d2.error));
                    }
                    var f2 = this.getMatchedCriteria(o2, t4, e6), h2 = this.validateMatchedPayloads(f2, e6), p2 = this.selectPayload(h2);
                    if (p2) {
                      var v2 = n3.getVariantAssignment(p2);
                      if (v2.ok) {
                        var g2 = v2.value;
                        return this.emitAssignment(g2), r4 && this.emitTrace(e6, "Experiment variant assigned: ".concat(g2.variant)), g2;
                      }
                      this.emitError(e6, "Experiment assignment failed: ".concat(v2.error));
                    } else r4 && this.emitTrace(e6, "No matching criteria from ".concat(o2.length, " experiment payloads"));
                  } else r4 && this.emitTrace(e6, "No experiment payloads exist for key");
                }, e5.prototype.getMatchedCriteria = function(e6, t4, r4) {
                  for (var n3 = new s.CriteriaInputs(t4), i2 = [], o2 = 0, u2 = e6; o2 < u2.length; o2++) {
                    var c2 = u2[o2], l2 = a.CriteriaParser.evaluateCriteria(c2, n3);
                    l2.ok ? l2.value.matched && i2.push(l2.value.payload) : this.emitError(r4, l2.error);
                  }
                  return i2;
                }, e5.prototype.validateMatchedPayloads = function(e6, t4) {
                  for (var r4 = [], n3 = 0, i2 = e6; n3 < i2.length; n3++) {
                    var o2 = i2[n3];
                    if (o2) {
                      var a2 = (0, l.validateSchemaAndReturn)(o2, h.DeviceConfigRolloutSchema);
                      a2.ok ? r4.push(a2.value) : this.emitError(t4, "Rollout payload invalid: ".concat(a2.error));
                    }
                  }
                  return r4;
                }, e5.prototype.selectPayload = function(e6) {
                  return e6.length > 0 ? e6[0] : void 0;
                }, e5.prototype.getFetchedSecondsAgo = function() {
                  if (this.dataFetchedWhenMs) return (this.clock.getMillis() - this.dataFetchedWhenMs) / 1e3;
                }, e5.prototype.debugLog = function(e6) {
                  this.enableConsoleLog && console.log("[DeviceConfig] ".concat(e6));
                }, e5;
              })();
              t3.DeviceConfigPropertyHolder = g;
              var m = (function() {
                function e5(e6) {
                  this.context = e6;
                }
                return e5.prototype.fetchUrl = function(e6, t4) {
                  var r4 = this, n3 = { headers: t4, cache: "no-store" }, i2 = null;
                  if ("undefined" != typeof AbortController) {
                    var o2 = new AbortController();
                    i2 = this.context.setTimeout(function() {
                      return o2.abort();
                    }, 1e3 * u.DEFAULT_HTTP_TIMEOUT_SECONDS), n3.signal = o2.signal;
                  }
                  return new Promise(function(t5, o3) {
                    r4.context.fetch(e6, n3).then(function(e7) {
                      if (e7.status >= 400) o3(new p.HttpError("http error ".concat(e7.status, ", ").concat(e7.statusText)));
                      else {
                        var r5 = { status: e7.status, headers: e7.headers };
                        e7.json().then(function(e8) {
                          r5.json = e8, t5(r5);
                        }).catch(function() {
                          t5(r5);
                        });
                      }
                    }).catch(function(e7) {
                      o3(e7);
                    }).finally(function() {
                      i2 && r4.context.clearTimeout(i2);
                    });
                  });
                }, e5;
              })();
            }, 3864: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), i(r3(363), t3), i(r3(4811), t3), i(r3(7094), t3), i(r3(3259), t3), i(r3(3840), t3), i(r3(6270), t3);
            }, 9456: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), i(r3(8903), t3);
            }, 8903: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.TypedEmitter = void 0;
              var i = r3(228), o = (function() {
                function e5(e6) {
                  var t4;
                  this.emitter = new i.EventEmitter(), this.propagateErrors = null !== (t4 = null == e6 ? void 0 : e6.propagateErrors) && void 0 !== t4 && t4;
                }
                return e5.prototype.on = function(e6, t4) {
                  t4.call = this.wrapCall(e6, t4), this.emitter.on(e6, t4);
                }, e5.prototype.off = function(e6, t4) {
                  this.emitter.off(e6, t4);
                }, e5.prototype.emit = function(e6) {
                  for (var t4, r4 = [], i2 = 1; i2 < arguments.length; i2++) r4[i2 - 1] = arguments[i2];
                  (t4 = this.emitter).emit.apply(t4, n2([e6], r4, false));
                }, e5.prototype.removeAllListeners = function() {
                  this.emitter.removeAllListeners();
                }, e5.prototype.wrapCall = function(e6, t4) {
                  var r4 = this;
                  return function(n3) {
                    for (var i2 = [], o2 = 1; o2 < arguments.length; o2++) i2[o2 - 1] = arguments[o2];
                    if (r4.propagateErrors) t4.apply(n3, i2);
                    else try {
                      t4.apply(n3, i2);
                    } catch (r5) {
                      var a = "Error in callback for ".concat(e6), s = t4.name;
                      return s && (a += " for function ".concat(s)), void console.error(a, r5);
                    }
                  };
                }, e5;
              })();
              t3.TypedEmitter = o;
            }, 5987: function(e4, t3, r3) {
              "use strict";
              var n2, i = this && this.__extends || (n2 = function(e5, t4) {
                return n2 = Object.setPrototypeOf || { __proto__: [] } instanceof Array && function(e6, t5) {
                  e6.__proto__ = t5;
                } || function(e6, t5) {
                  for (var r4 in t5) Object.prototype.hasOwnProperty.call(t5, r4) && (e6[r4] = t5[r4]);
                }, n2(e5, t4);
              }, function(e5, t4) {
                if ("function" != typeof t4 && null !== t4) throw new TypeError("Class extends value " + String(t4) + " is not a constructor or null");
                function r4() {
                  this.constructor = e5;
                }
                n2(e5, t4), e5.prototype = null === t4 ? Object.create(t4) : (r4.prototype = t4.prototype, new r4());
              }), o = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), a = this && this.__setModuleDefault || (Object.create ? function(e5, t4) {
                Object.defineProperty(e5, "default", { enumerable: true, value: t4 });
              } : function(e5, t4) {
                e5.default = t4;
              }), s = this && this.__importStar || function(e5) {
                if (e5 && e5.__esModule) return e5;
                var t4 = {};
                if (null != e5) for (var r4 in e5) "default" !== r4 && Object.prototype.hasOwnProperty.call(e5, r4) && o(t4, e5, r4);
                return a(t4, e5), t4;
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createFsm = t3.FsmWildcardState = t3.RejectedTransitionError = t3.InvalidStateTransitionError = void 0;
              var u = s(r3(4293)), c = (function(e5) {
                function t4(t5, r4) {
                  var n3 = e5.call(this, "Invalid transition from '".concat(r4, "' via event '").concat(t5, "'")) || this;
                  return n3.name = "InvalidStateTransitionError", n3;
                }
                return i(t4, e5), t4;
              })(Error);
              t3.InvalidStateTransitionError = c;
              var l = (function(e5) {
                function t4(t5, r4, n3) {
                  var i2 = e5.call(this, "Transition from '".concat(r4, "' via event '").concat(t5, "' was rejected for: ").concat(n3)) || this;
                  return i2.name = "RejectedTransitionError", i2;
                }
                return i(t4, e5), t4;
              })(Error);
              t3.RejectedTransitionError = l, t3.FsmWildcardState = "*", t3.createFsm = function(e5) {
                var r4, n3 = e5.transitions, i2 = e5.callbacks, o2 = e5.initialState, a2 = function(e6) {
                  var r5, i3 = null === (r5 = n3[e6]) || void 0 === r5 ? void 0 : r5.from;
                  return !!i3 && ("string" == typeof i3 ? i3 === t3.FsmWildcardState : i3.includes(o2));
                };
                return { canTransition: a2, transition: function(e6, t4) {
                  var s2, d, f, h, p, v;
                  if (!a2(e6)) return u.err(new c(String(o2), String(e6)));
                  var g = n3[e6];
                  return void 0 === g.shouldTransition || g.shouldTransition(o2) ? (null === (d = null === (s2 = null == i2 ? void 0 : i2[o2]) || void 0 === s2 ? void 0 : s2.onExiting) || void 0 === d || d.call(s2), null === (f = null == i2 ? void 0 : i2.onStateExiting) || void 0 === f || f.call(i2, o2), r4 = o2, o2 = g.to, null === (p = null === (h = null == i2 ? void 0 : i2[o2]) || void 0 === h ? void 0 : h.onChanged) || void 0 === p || p.call(h, r4, t4), null === (v = null == i2 ? void 0 : i2.onStateChanged) || void 0 === v || v.call(i2, r4, o2, t4), u.ok(o2)) : u.err(new l(String(o2), String(e6), "shouldTransition returned false"));
                }, previousState: function() {
                  return r4;
                }, currentState: function() {
                  return o2;
                } };
              };
            }, 8156: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.VERSION = void 0, t3.VERSION = "0.16.0", i(r3(3864), t3), i(r3(9456), t3), i(r3(5987), t3), i(r3(9266), t3), i(r3(6925), t3), i(r3(5224), t3);
            }, 6925: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), i(r3(6166), t3), i(r3(7612), t3), i(r3(7056), t3), i(r3(8139), t3), i(r3(3877), t3);
            }, 6166: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__assign || function() {
                return n2 = Object.assign || function(e5) {
                  for (var t4, r4 = 1, n3 = arguments.length; r4 < n3; r4++) for (var i2 in t4 = arguments[r4]) Object.prototype.hasOwnProperty.call(t4, i2) && (e5[i2] = t4[i2]);
                  return e5;
                }, n2.apply(this, arguments);
              }, i = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.getLogConfig = t3.setLogConfig = t3.setLogConfigByCategories = t3.setLogConfigByLevel = t3.extractLogConfig = void 0;
              var o = r3(7612), a = r3(7056), s = r3(8139), u = r3(4478), c = { enabled: false, levels: { debug: true, log: true, info: true, warn: true, error: true }, targets: [(0, a.createConsoleLogTarget)()], categories: {} }, l = Object.keys(c);
              t3.extractLogConfig = function(e5) {
                var t4 = {};
                if (Object.keys(e5).some(function(e6) {
                  return l.includes(e6);
                })) {
                  for (var r4 = 0, n3 = l; r4 < n3.length; r4++) {
                    var i2 = n3[r4];
                    if (void 0 !== e5[i2]) {
                      var o2 = e5[i2];
                      t4[i2] = o2;
                    }
                  }
                  return t4;
                }
              }, t3.setLogConfigByLevel = function(e5) {
                var r4 = e5.enabled || c.enabled, n3 = e5.targets || c.targets, i2 = { debug: false, log: false, info: false, warn: false, error: false }, o2 = function(e6, t4) {
                  for (var r5 = 0, n4 = e6; r5 < n4.length; r5++) {
                    var o3 = n4[r5];
                    i2[o3] = t4;
                  }
                };
                switch (e5.level) {
                  case "debug":
                    o2(["debug", "log", "info", "warn", "error"], r4);
                    break;
                  case "log":
                    o2(["log", "info", "warn", "error"], r4);
                    break;
                  case "info":
                    o2(["info", "warn", "error"], true);
                    break;
                  case "warn":
                    o2(["warn", "error"], true);
                    break;
                  case "error":
                    o2(["error"], true);
                }
                (0, t3.setLogConfig)({ enabled: r4, levels: i2, targets: n3 });
              }, t3.setLogConfigByCategories = function(e5, r4) {
                void 0 === r4 && (r4 = true);
                var n3 = {};
                if (Array.isArray(e5)) for (var i2 = 0, o2 = e5; i2 < o2.length; i2++) n3[o2[i2]] = true;
                else n3 = e5;
                r4 && (c.categories = {}), (0, t3.setLogConfig)({ categories: n3 });
              }, t3.setLogConfig = function(e5) {
                var t4 = (0, u.mergeConfigs)(c, e5);
                t4 && (c = t4, o.defaultLogEmitter.emit(s.LogEventType.CONFIG_UPDATED, c));
              }, t3.getLogConfig = function() {
                return { enabled: c.enabled, levels: n2({}, c.levels), targets: i([], c.targets, true), categories: n2({}, c.categories) };
              };
            }, 7612: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.defaultLogEmitter = void 0;
              var n2 = r3(9456);
              t3.defaultLogEmitter = new n2.TypedEmitter({});
            }, 7056: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o = t4.length; i2 < o; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createEventLogTarget = t3.createConsoleLogTarget = void 0;
              var i = r3(8139);
              t3.createConsoleLogTarget = function() {
                return { handleMessage: function(e5) {
                  for (var t4 = [], r4 = 1; r4 < arguments.length; r4++) t4[r4 - 1] = arguments[r4];
                  console[e5].apply(console, t4);
                } };
              }, t3.createEventLogTarget = function(e5) {
                return { handleMessage: function(t4) {
                  for (var r4 = [], o = 1; o < arguments.length; o++) r4[o - 1] = arguments[o];
                  e5.emit.apply(e5, n2([i.LogEventType.MESSAGE_LOGGED, t4], r4, false));
                } };
              };
            }, 8139: function(e4, t3) {
              "use strict";
              var r3;
              Object.defineProperty(t3, "__esModule", { value: true }), t3.LogEventType = void 0, (function(e5) {
                e5.CONFIG_UPDATED = "logConfigUpdated", e5.MESSAGE_LOGGED = "messageLogged";
              })(r3 || (t3.LogEventType = r3 = {}));
            }, 3877: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__assign || function() {
                return n2 = Object.assign || function(e5) {
                  for (var t4, r4 = 1, n3 = arguments.length; r4 < n3; r4++) for (var i2 in t4 = arguments[r4]) Object.prototype.hasOwnProperty.call(t4, i2) && (e5[i2] = t4[i2]);
                  return e5;
                }, n2.apply(this, arguments);
              }, i = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createLogger = void 0;
              var o = r3(4478), a = r3(6166), s = r3(7612), u = r3(8139), c = function(e5, t4) {
                return (0, o.mergeConfigs)(e5, t4) || e5;
              };
              t3.createLogger = function(e5) {
                var r4, o2 = e5.name, l = e5.category, d = e5.parent, f = (0, a.extractLogConfig)(e5) || {}, h = c((0, a.getLogConfig)(), f), p = function() {
                  var e6 = h.enabled && h.targets.length > 0 && (void 0 === l || (function(e7, t4) {
                    if (t4[e7]) return true;
                    for (var r5 = e7.split("."), n3 = r5.length - 1; n3 > 0; n3--) if (t4[r5.slice(0, n3).join(".")]) return true;
                    return false;
                  })(l, h.categories));
                  r4 = { debug: e6 && h.levels.debug, log: e6 && h.levels.log, info: e6 && h.levels.info, warn: e6 && h.levels.warn, error: e6 && h.levels.error };
                };
                p(), s.defaultLogEmitter.on(u.LogEventType.CONFIG_UPDATED, function(e6) {
                  h = c(e6, f || {}), p();
                });
                var v = function(e6) {
                  for (var t4 = [], r5 = 1; r5 < arguments.length; r5++) t4[r5 - 1] = arguments[r5];
                  if (d && !d.canLog(e6)) return false;
                  if (!m(e6)) return false;
                  for (var n3 = g(), o3 = (/* @__PURE__ */ new Date()).toTimeString().split(" ")[0], a2 = i(["".concat(o3, " - (").concat(n3, ")")], t4, true), s2 = 0, u2 = h.targets; s2 < u2.length; s2++) {
                    var c2 = u2[s2];
                    c2.handleMessage.apply(c2, i([e6], a2, false));
                  }
                  return true;
                }, g = function() {
                  return d ? "".concat(d.getName(), " | ").concat(o2) : o2;
                }, m = function(e6) {
                  return r4[e6];
                }, y = function(e6) {
                  h = c(h, f = e6), p();
                }, b = { debug: function() {
                  for (var e6 = [], t4 = 0; t4 < arguments.length; t4++) e6[t4] = arguments[t4];
                  return v.apply(void 0, i(["debug"], e6, false));
                }, log: function() {
                  for (var e6 = [], t4 = 0; t4 < arguments.length; t4++) e6[t4] = arguments[t4];
                  return v.apply(void 0, i(["log"], e6, false));
                }, info: function() {
                  for (var e6 = [], t4 = 0; t4 < arguments.length; t4++) e6[t4] = arguments[t4];
                  return v.apply(void 0, i(["info"], e6, false));
                }, warn: function() {
                  for (var e6 = [], t4 = 0; t4 < arguments.length; t4++) e6[t4] = arguments[t4];
                  return v.apply(void 0, i(["warn"], e6, false));
                }, error: function() {
                  for (var e6 = [], t4 = 0; t4 < arguments.length; t4++) e6[t4] = arguments[t4];
                  return v.apply(void 0, i(["error"], e6, false));
                }, getName: g, canLog: m, getChildLogger: function(e6) {
                  return (0, t3.createLogger)(n2(n2({}, e6), { parent: b }));
                }, setConfig: y, setConfigByLevel: function(e6) {
                  var t4 = { debug: false, log: false, info: false, warn: false, error: false }, r5 = function(e7) {
                    for (var r6 = 0, n3 = e7; r6 < n3.length; r6++) {
                      var i2 = n3[r6];
                      t4[i2] = true;
                    }
                  };
                  switch (e6) {
                    case "debug":
                      r5(["debug", "log", "info", "warn", "error"]);
                      break;
                    case "log":
                      r5(["log", "info", "warn", "error"]);
                      break;
                    case "info":
                      r5(["info", "warn", "error"]);
                      break;
                    case "warn":
                      r5(["warn", "error"]);
                      break;
                    case "error":
                      r5(["error"]);
                  }
                  y({ levels: t4 });
                } };
                return b;
              };
            }, 5224: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), i(r3(3443), t3), i(r3(6468), t3);
            }, 413: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createExecutorNode = void 0;
              var n2 = r3(3431), i = function(e5) {
                return "succeeded" === e5 || "failed" === e5 || "canceled" === e5;
              }, o = function(e5) {
                for (var t4 = e5; t4.parent; ) t4 = t4.parent;
                return t4;
              }, a = function(e5, t4) {
                var r4, i2 = (0, n2.transition)(e5.state, ["pending", "running"], "failed");
                i2 ? (e5.state = i2, e5.settledError = t4, (0, n2.notifyFailure)(e5.deferStack, e5.failureCBs, e5.completedCBs, t4), s(e5.child, t4)) : null === (r4 = e5.logger) || void 0 === r4 || r4.debug("suppressing failure, state is", e5.state);
              }, s = function(e5, t4) {
                e5 && (0, n2.transition)(e5.state, ["pending"], "failed") && (e5.state = "failed", e5.settledError = t4, (0, n2.notifyFailure)(e5.deferStack, e5.failureCBs, e5.completedCBs, t4), s(e5.child, t4));
              }, u = function(e5) {
                if (e5) {
                  var t4 = (0, n2.transition)(e5.state, ["pending", "running"], "canceling");
                  t4 && (e5.state = t4), u(e5.child);
                }
              }, c = function(e5) {
                e5 && ((0, n2.transition)(e5.state, ["canceling"], "canceled") && (e5.state = "canceled", (0, n2.notifyCancel)(e5.cancelHandlers, e5.deferStack, e5.canceledCBs, e5.completedCBs)), c(e5.parent));
              }, l = function(e5, t4, r4) {
                if ((0, n2.transition)(e5.state, ["pending"], "running")) {
                  e5.state = "running";
                  var i2 = function(t5) {
                    (function(e6, t6) {
                      var r5, i3 = (0, n2.transition)(e6.state, ["running"], "succeeded");
                      return i3 ? (e6.state = i3, e6.settledResult = t6, (0, n2.notifySuccess)(e6.deferStack, e6.successCBs, e6.completedCBs, t6), true) : (null === (r5 = e6.logger) || void 0 === r5 || r5.debug("suppressing success, state is", e6.state), false);
                    })(e5, t5) && e5.child && (function(e6, t6) {
                      if (e6 === t6) return true;
                      for (var r5 = t6; r5; ) {
                        if (r5 === e6) return true;
                        r5 = r5.parent;
                      }
                      return false;
                    })(e5, r4) && l(e5.child, t5, r4);
                  };
                  try {
                    var o2 = e5.func(t4, e5.context);
                    o2 instanceof Promise ? o2.then(i2).catch(function(t5) {
                      return a(e5, (0, n2.normalizeError)(t5));
                    }) : i2(o2);
                  } catch (t5) {
                    a(e5, (0, n2.normalizeError)(t5));
                  }
                }
              }, d = function(e5, t4, r4) {
                var n3 = { state: "pending", func: e5, parent: t4, child: void 0, logger: r4, context: { isCanceled: function() {
                  return "canceling" === n3.state || "canceled" === n3.state;
                }, defer: function(e6) {
                  n3.deferStack.push(e6);
                }, onCancel: function(e6) {
                  n3.cancelHandlers.push(e6);
                } }, settledResult: void 0, settledError: void 0, deferStack: [], cancelHandlers: [], successCBs: [], failureCBs: [], canceledCBs: [], completedCBs: [] };
                return t4 && (t4.child = n3), n3;
              }, f = function(e5, t4) {
                var r4 = { run: function() {
                  var n3 = o(e5);
                  return l(n3, void 0, t4), r4;
                }, fold: function(t5, n3, i2) {
                  return "succeeded" === e5.state ? t5(e5.settledResult) : "failed" === e5.state ? n3(e5.settledError) : "canceled" === e5.state && i2 ? i2() : (e5.successCBs.push(t5), e5.failureCBs.push(n3), i2 && e5.canceledCBs.push(i2)), r4;
                }, chain: function(t5) {
                  var r5 = d(function(e6, r6) {
                    return t5(e6, r6);
                  }, e5, e5.logger);
                  return f(r5, r5);
                }, cancel: function() {
                  return (function(e6) {
                    var t5 = o(e6);
                    u(t5);
                    var r5 = (function(e7) {
                      for (var t6 = e7; t6.child; ) t6 = t6.child;
                      return t6;
                    })(e6);
                    c(r5);
                  })(e5), r4;
                }, fail: function(t5) {
                  return a(e5, t5), r4;
                }, isCompleted: function() {
                  return i(e5.state);
                }, onCompleted: function(t5) {
                  return i(e5.state) ? t5() : e5.completedCBs.push(t5), r4;
                } };
                return r4;
              };
              t3.createExecutorNode = function(e5, t4) {
                var r4, n3 = null === (r4 = null == t4 ? void 0 : t4.logger) || void 0 === r4 ? void 0 : r4.getChildLogger({ name: "Task" }), i2 = d(function(t5, r5) {
                  return e5(r5);
                }, void 0, n3);
                return f(i2, i2);
              };
            }, 6468: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__awaiter || function(e5, t4, r4, n3) {
                return new (r4 || (r4 = Promise))(function(i2, o2) {
                  function a2(e6) {
                    try {
                      u(n3.next(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function s(e6) {
                    try {
                      u(n3.throw(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function u(e6) {
                    var t5;
                    e6.done ? i2(e6.value) : (t5 = e6.value, t5 instanceof r4 ? t5 : new r4(function(e7) {
                      e7(t5);
                    })).then(a2, s);
                  }
                  u((n3 = n3.apply(e5, t4 || [])).next());
                });
              }, i = this && this.__generator || function(e5, t4) {
                var r4, n3, i2, o2, a2 = { label: 0, sent: function() {
                  if (1 & i2[0]) throw i2[1];
                  return i2[1];
                }, trys: [], ops: [] };
                return o2 = { next: s(0), throw: s(1), return: s(2) }, "function" == typeof Symbol && (o2[Symbol.iterator] = function() {
                  return this;
                }), o2;
                function s(s2) {
                  return function(u) {
                    return (function(s3) {
                      if (r4) throw new TypeError("Generator is already executing.");
                      for (; o2 && (o2 = 0, s3[0] && (a2 = 0)), a2; ) try {
                        if (r4 = 1, n3 && (i2 = 2 & s3[0] ? n3.return : s3[0] ? n3.throw || ((i2 = n3.return) && i2.call(n3), 0) : n3.next) && !(i2 = i2.call(n3, s3[1])).done) return i2;
                        switch (n3 = 0, i2 && (s3 = [2 & s3[0], i2.value]), s3[0]) {
                          case 0:
                          case 1:
                            i2 = s3;
                            break;
                          case 4:
                            return a2.label++, { value: s3[1], done: false };
                          case 5:
                            a2.label++, n3 = s3[1], s3 = [0];
                            continue;
                          case 7:
                            s3 = a2.ops.pop(), a2.trys.pop();
                            continue;
                          default:
                            if (!((i2 = (i2 = a2.trys).length > 0 && i2[i2.length - 1]) || 6 !== s3[0] && 2 !== s3[0])) {
                              a2 = 0;
                              continue;
                            }
                            if (3 === s3[0] && (!i2 || s3[1] > i2[0] && s3[1] < i2[3])) {
                              a2.label = s3[1];
                              break;
                            }
                            if (6 === s3[0] && a2.label < i2[1]) {
                              a2.label = i2[1], i2 = s3;
                              break;
                            }
                            if (i2 && a2.label < i2[2]) {
                              a2.label = i2[2], a2.ops.push(s3);
                              break;
                            }
                            i2[2] && a2.ops.pop(), a2.trys.pop();
                            continue;
                        }
                        s3 = t4.call(e5, a2);
                      } catch (e6) {
                        s3 = [6, e6], n3 = 0;
                      } finally {
                        r4 = i2 = 0;
                      }
                      if (5 & s3[0]) throw s3[1];
                      return { value: s3[0] ? s3[1] : void 0, done: true };
                    })([s2, u]);
                  };
                }
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.LiveTask = t3.Task = void 0;
              var o = r3(6787), a = r3(2086);
              t3.Task = { create: function(e5) {
                return (0, o.createTask)(e5);
              }, run: function(e5, t4, r4, n3) {
                return (0, o.createTask)(e5).fold(t4, r4, n3).run();
              }, from: function(e5) {
                return (0, o.createTask)(function() {
                  return e5;
                });
              }, chain: function(e5) {
                for (var t4 = [], r4 = 1; r4 < arguments.length; r4++) t4[r4 - 1] = arguments[r4];
                for (var n3 = (0, o.createTask)(e5), i2 = 0, a2 = t4; i2 < a2.length; i2++) {
                  var s = a2[i2];
                  n3 = n3.chain(s);
                }
                return n3;
              }, sequence: function(e5) {
                var t4 = this;
                return (0, o.createTask)(function(r4) {
                  return n2(t4, void 0, void 0, function() {
                    var t5, n3, o2, a2, s;
                    return i(this, function(i2) {
                      switch (i2.label) {
                        case 0:
                          t5 = [], n3 = 0, o2 = e5, i2.label = 1;
                        case 1:
                          return n3 < o2.length ? (a2 = o2[n3], r4.isCanceled() ? [3, 4] : [4, a2(r4)]) : [3, 4];
                        case 2:
                          s = i2.sent(), t5.push(s), i2.label = 3;
                        case 3:
                          return n3++, [3, 1];
                        case 4:
                          return [2, t5];
                      }
                    });
                  });
                });
              }, all: function(e5) {
                return (0, o.createTask)(function(t4) {
                  return new Promise(function(r4, n3) {
                    var i2 = new Array(e5.length), a2 = e5.length, s = false, u = [], c = function() {
                      for (var e6 = 0, t5 = u; e6 < t5.length; e6++) t5[e6].cancel();
                    };
                    t4.onCancel(c), e5.forEach(function(e6, t5) {
                      var l = (0, o.createTask)(e6).fold(function(e7) {
                        s || (i2[t5] = e7, 0 === --a2 && (s = true, r4(i2)));
                      }, function(e7) {
                        s || (s = true, c(), n3(e7));
                      }).run();
                      u.push(l);
                    });
                  });
                });
              }, race: function(e5) {
                return (0, o.createTask)(function(t4) {
                  return new Promise(function(r4, n3) {
                    var i2 = false, a2 = [], s = function() {
                      for (var e6 = 0, t5 = a2; e6 < t5.length; e6++) t5[e6].cancel();
                    };
                    t4.onCancel(s);
                    for (var u = 0, c = e5; u < c.length; u++) {
                      var l = c[u], d = (0, o.createTask)(l).fold(function(e6) {
                        i2 || (i2 = true, s(), r4(e6));
                      }, function(e6) {
                        i2 || (i2 = true, s(), n3(e6));
                      }).run();
                      a2.push(d);
                    }
                  });
                });
              } }, t3.LiveTask = { create: function(e5) {
                return (0, a.createLiveTask)(e5);
              } };
            }, 2086: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createLiveTask = void 0;
              var n2 = r3(3431);
              t3.createLiveTask = function(e5, t4) {
                var r4, i, o, a = null === (r4 = null == t4 ? void 0 : t4.logger) || void 0 === r4 ? void 0 : r4.getChildLogger({ name: "LiveTask" }), s = "pending", u = [], c = [], l = [], d = [], f = [], h = [], p = { isCanceled: function() {
                  return "canceling" === s || "canceled" === s;
                }, defer: function(e6) {
                  u.push(e6);
                }, onCancel: function(e6) {
                  c.push(e6);
                } }, v = function() {
                  return "failed" === s || "canceled" === s || "completed" === s;
                }, g = function(e6) {
                  if ((0, n2.transition)(s, ["running"], "live")) {
                    s = "live", i = e6;
                    for (var t5 = 0, r5 = l; t5 < r5.length; t5++) (0, r5[t5])(e6);
                    l.length = 0;
                  } else null == a || a.debug("suppressing live, state is", s);
                }, m = function(e6) {
                  (0, n2.transition)(s, ["pending", "running", "live"], "failed") ? (s = "failed", o = e6, (0, n2.notifyFailure)(u, d, h, e6)) : null == a || a.debug("suppressing failure, state is", s);
                }, y = { run: function() {
                  if (!(0, n2.transition)(s, ["pending"], "running")) return y;
                  s = "running";
                  try {
                    var t5 = e5(p);
                    t5 instanceof Promise ? t5.then(function(e6) {
                      return g(e6);
                    }).catch(function(e6) {
                      return m((0, n2.normalizeError)(e6));
                    }) : g(t5);
                  } catch (e6) {
                    m((0, n2.normalizeError)(e6));
                  }
                  return y;
                }, use: function(e6) {
                  if ("live" === s) try {
                    e6(i);
                  } catch (e7) {
                    m((0, n2.normalizeError)(e7));
                  }
                  else v() || l.push(e6);
                  return y;
                }, done: function() {
                  return (function() {
                    if ((0, n2.transition)(s, ["live"], "completed")) {
                      s = "completed", (0, n2.runGuarded)(u);
                      for (var e6 = 0, t5 = h; e6 < t5.length; e6++) (0, t5[e6])();
                    }
                  })(), y;
                }, cancel: function() {
                  return (0, n2.transition)(s, ["pending", "running", "live"], "canceled") ? (s = "canceled", (0, n2.notifyCancel)(c, u, f, h)) : null == a || a.debug("suppressing cancel, state is", s), y;
                }, fail: function(e6) {
                  return m(e6), y;
                }, isLive: function() {
                  return "live" === s;
                }, onCanceled: function(e6) {
                  return "canceled" === s ? e6() : f.push(e6), y;
                }, onFailed: function(e6) {
                  return "failed" === s ? e6(o) : d.push(e6), y;
                }, onCompleted: function(e6) {
                  return v() ? e6() : h.push(e6), y;
                } };
                return y;
              };
            }, 3443: function(e4, t3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true });
            }, 3431: function(e4, t3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.notifyCancel = t3.notifyFailure = t3.notifySuccess = t3.runGuarded = t3.transition = t3.normalizeError = void 0, t3.normalizeError = function(e5) {
                return e5 instanceof Error ? e5 : new Error(String(e5));
              }, t3.transition = function(e5, t4, r3) {
                return !!t4.includes(e5) && r3;
              }, t3.runGuarded = function(e5) {
                for (var t4 = 0, r3 = e5; t4 < r3.length; t4++) {
                  var n2 = r3[t4];
                  try {
                    n2();
                  } catch (e6) {
                  }
                }
              }, t3.notifySuccess = function(e5, r3, n2, i) {
                (0, t3.runGuarded)(e5);
                for (var o = 0, a = r3; o < a.length; o++) (0, a[o])(i);
                for (var s = 0, u = n2; s < u.length; s++) (0, u[s])();
              }, t3.notifyFailure = function(e5, r3, n2, i) {
                (0, t3.runGuarded)(e5);
                for (var o = 0, a = r3; o < a.length; o++) (0, a[o])(i);
                for (var s = 0, u = n2; s < u.length; s++) (0, u[s])();
              }, t3.notifyCancel = function(e5, r3, n2, i) {
                (0, t3.runGuarded)(e5), (0, t3.runGuarded)(r3);
                for (var o = 0, a = n2; o < a.length; o++) (0, a[o])();
                for (var s = 0, u = i; s < u.length; s++) (0, u[s])();
              };
            }, 6787: function(e4, t3, r3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.createTask = void 0;
              var n2 = r3(413);
              t3.createTask = function(e5, t4) {
                return (0, n2.createExecutorNode)(e5, t4);
              };
            }, 8177: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__setModuleDefault || (Object.create ? function(e5, t4) {
                Object.defineProperty(e5, "default", { enumerable: true, value: t4 });
              } : function(e5, t4) {
                e5.default = t4;
              }), o = this && this.__importStar || function(e5) {
                if (e5 && e5.__esModule) return e5;
                var t4 = {};
                if (null != e5) for (var r4 in e5) "default" !== r4 && Object.prototype.hasOwnProperty.call(e5, r4) && n2(t4, e5, r4);
                return i(t4, e5), t4;
              }, a = this && this.__awaiter || function(e5, t4, r4, n3) {
                return new (r4 || (r4 = Promise))(function(i2, o2) {
                  function a2(e6) {
                    try {
                      u2(n3.next(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function s2(e6) {
                    try {
                      u2(n3.throw(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function u2(e6) {
                    var t5;
                    e6.done ? i2(e6.value) : (t5 = e6.value, t5 instanceof r4 ? t5 : new r4(function(e7) {
                      e7(t5);
                    })).then(a2, s2);
                  }
                  u2((n3 = n3.apply(e5, t4 || [])).next());
                });
              }, s = this && this.__generator || function(e5, t4) {
                var r4, n3, i2, o2, a2 = { label: 0, sent: function() {
                  if (1 & i2[0]) throw i2[1];
                  return i2[1];
                }, trys: [], ops: [] };
                return o2 = { next: s2(0), throw: s2(1), return: s2(2) }, "function" == typeof Symbol && (o2[Symbol.iterator] = function() {
                  return this;
                }), o2;
                function s2(s3) {
                  return function(u2) {
                    return (function(s4) {
                      if (r4) throw new TypeError("Generator is already executing.");
                      for (; o2 && (o2 = 0, s4[0] && (a2 = 0)), a2; ) try {
                        if (r4 = 1, n3 && (i2 = 2 & s4[0] ? n3.return : s4[0] ? n3.throw || ((i2 = n3.return) && i2.call(n3), 0) : n3.next) && !(i2 = i2.call(n3, s4[1])).done) return i2;
                        switch (n3 = 0, i2 && (s4 = [2 & s4[0], i2.value]), s4[0]) {
                          case 0:
                          case 1:
                            i2 = s4;
                            break;
                          case 4:
                            return a2.label++, { value: s4[1], done: false };
                          case 5:
                            a2.label++, n3 = s4[1], s4 = [0];
                            continue;
                          case 7:
                            s4 = a2.ops.pop(), a2.trys.pop();
                            continue;
                          default:
                            if (!((i2 = (i2 = a2.trys).length > 0 && i2[i2.length - 1]) || 6 !== s4[0] && 2 !== s4[0])) {
                              a2 = 0;
                              continue;
                            }
                            if (3 === s4[0] && (!i2 || s4[1] > i2[0] && s4[1] < i2[3])) {
                              a2.label = s4[1];
                              break;
                            }
                            if (6 === s4[0] && a2.label < i2[1]) {
                              a2.label = i2[1], i2 = s4;
                              break;
                            }
                            if (i2 && a2.label < i2[2]) {
                              a2.label = i2[2], a2.ops.push(s4);
                              break;
                            }
                            i2[2] && a2.ops.pop(), a2.trys.pop();
                            continue;
                        }
                        s4 = t4.call(e5, a2);
                      } catch (e6) {
                        s4 = [6, e6], n3 = 0;
                      } finally {
                        r4 = i2 = 0;
                      }
                      if (5 & s4[0]) throw s4[1];
                      return { value: s4[0] ? s4[1] : void 0, done: true };
                    })([s3, u2]);
                  };
                }
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.generateUuid = t3.getSecureRandom = t3.UuidMethod = t3.SecureRandomMethod = t3.hashProperty = void 0;
              var u, c, l = o(r3(4293));
              function d() {
                if ("undefined" != typeof crypto && crypto.getRandomValues) try {
                  var e5 = new Uint32Array(1);
                  return crypto.getRandomValues(e5), { value: e5[0] / 4294967296, method: u.CRYPTO_RANDOM_VALUES };
                } catch (e6) {
                }
                return { value: Math.random(), method: u.MATH_RANDOM };
              }
              t3.hashProperty = function(e5) {
                return a(void 0, void 0, void 0, function() {
                  var t4, r4, n3, i2, o2;
                  return s(this, function(a2) {
                    switch (a2.label) {
                      case 0:
                        if ("undefined" == typeof crypto) return [2, l.err("WebCrypto is unsupported, skipping hash")];
                        if ("function" != typeof (null === crypto || void 0 === crypto ? void 0 : crypto.subtle.digest)) return [2, l.err("WebCrypto subtle.digest does not exist, possible insecure origin")];
                        a2.label = 1;
                      case 1:
                        return a2.trys.push([1, 3, , 4]), t4 = new TextEncoder().encode(e5), [4, crypto.subtle.digest("SHA-256", t4)];
                      case 2:
                        return r4 = a2.sent(), n3 = Array.from(new Uint8Array(r4)), i2 = n3.map(function(e6) {
                          return e6.toString(16).padStart(2, "0");
                        }).join(""), [2, l.ok({ key: e5, hash: i2 })];
                      case 3:
                        return o2 = a2.sent(), [2, l.err("Unable to hash rule: ".concat(o2))];
                      case 4:
                        return [2];
                    }
                  });
                });
              }, (function(e5) {
                e5.CRYPTO_RANDOM_VALUES = "crypto-random-values", e5.MATH_RANDOM = "math-random";
              })(u || (t3.SecureRandomMethod = u = {})), (function(e5) {
                e5.CRYPTO_RANDOM_UUID = "crypto-random-uuid", e5.MANUAL_CRYPTO_RANDOM_VALUES = "manual-crypto-random-values", e5.MANUAL_MATH_RANDOM = "manual-math-random";
              })(c || (t3.UuidMethod = c = {})), t3.getSecureRandom = d, t3.generateUuid = function() {
                if ("undefined" != typeof crypto && crypto.randomUUID) try {
                  return { value: crypto.randomUUID(), method: c.CRYPTO_RANDOM_UUID };
                } catch (e6) {
                }
                for (var e5 = "0123456789abcdef", t4 = "", r4 = c.MANUAL_CRYPTO_RANDOM_VALUES, n3 = 0; n3 < 36; n3++) if (8 === n3 || 13 === n3 || 18 === n3 || 23 === n3) t4 += "-";
                else if (14 === n3) t4 += "4";
                else if (19 === n3) (i2 = d()).method === u.MATH_RANDOM && (r4 = c.MANUAL_MATH_RANDOM), t4 += e5[Math.floor(4 * i2.value) + 8];
                else {
                  var i2;
                  (i2 = d()).method === u.MATH_RANDOM && (r4 = c.MANUAL_MATH_RANDOM), t4 += e5[Math.floor(16 * i2.value)];
                }
                return { value: t4, method: r4 };
              };
            }, 6889: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__awaiter || function(e5, t4, r4, n3) {
                return new (r4 || (r4 = Promise))(function(i2, o2) {
                  function a(e6) {
                    try {
                      u(n3.next(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function s(e6) {
                    try {
                      u(n3.throw(e6));
                    } catch (e7) {
                      o2(e7);
                    }
                  }
                  function u(e6) {
                    var t5;
                    e6.done ? i2(e6.value) : (t5 = e6.value, t5 instanceof r4 ? t5 : new r4(function(e7) {
                      e7(t5);
                    })).then(a, s);
                  }
                  u((n3 = n3.apply(e5, t4 || [])).next());
                });
              }, i = this && this.__generator || function(e5, t4) {
                var r4, n3, i2, o2, a = { label: 0, sent: function() {
                  if (1 & i2[0]) throw i2[1];
                  return i2[1];
                }, trys: [], ops: [] };
                return o2 = { next: s(0), throw: s(1), return: s(2) }, "function" == typeof Symbol && (o2[Symbol.iterator] = function() {
                  return this;
                }), o2;
                function s(s2) {
                  return function(u) {
                    return (function(s3) {
                      if (r4) throw new TypeError("Generator is already executing.");
                      for (; o2 && (o2 = 0, s3[0] && (a = 0)), a; ) try {
                        if (r4 = 1, n3 && (i2 = 2 & s3[0] ? n3.return : s3[0] ? n3.throw || ((i2 = n3.return) && i2.call(n3), 0) : n3.next) && !(i2 = i2.call(n3, s3[1])).done) return i2;
                        switch (n3 = 0, i2 && (s3 = [2 & s3[0], i2.value]), s3[0]) {
                          case 0:
                          case 1:
                            i2 = s3;
                            break;
                          case 4:
                            return a.label++, { value: s3[1], done: false };
                          case 5:
                            a.label++, n3 = s3[1], s3 = [0];
                            continue;
                          case 7:
                            s3 = a.ops.pop(), a.trys.pop();
                            continue;
                          default:
                            if (!((i2 = (i2 = a.trys).length > 0 && i2[i2.length - 1]) || 6 !== s3[0] && 2 !== s3[0])) {
                              a = 0;
                              continue;
                            }
                            if (3 === s3[0] && (!i2 || s3[1] > i2[0] && s3[1] < i2[3])) {
                              a.label = s3[1];
                              break;
                            }
                            if (6 === s3[0] && a.label < i2[1]) {
                              a.label = i2[1], i2 = s3;
                              break;
                            }
                            if (i2 && a.label < i2[2]) {
                              a.label = i2[2], a.ops.push(s3);
                              break;
                            }
                            i2[2] && a.ops.pop(), a.trys.pop();
                            continue;
                        }
                        s3 = t4.call(e5, a);
                      } catch (e6) {
                        s3 = [6, e6], n3 = 0;
                      } finally {
                        r4 = i2 = 0;
                      }
                      if (5 & s3[0]) throw s3[1];
                      return { value: s3[0] ? s3[1] : void 0, done: true };
                    })([s2, u]);
                  };
                }
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.delay = void 0;
              var o = r3(6468);
              t3.delay = function(e5) {
                return o.Task.create(function(t4) {
                  return n2(void 0, void 0, void 0, function() {
                    return i(this, function(r4) {
                      return [2, new Promise(function(r5) {
                        var n3 = setTimeout(r5, e5);
                        t4.defer(function() {
                          clearTimeout(n3);
                        });
                      })];
                    });
                  });
                });
              };
            }, 9266: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__createBinding || (Object.create ? function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4);
                var i2 = Object.getOwnPropertyDescriptor(t4, r4);
                i2 && !("get" in i2 ? !t4.__esModule : i2.writable || i2.configurable) || (i2 = { enumerable: true, get: function() {
                  return t4[r4];
                } }), Object.defineProperty(e5, n3, i2);
              } : function(e5, t4, r4, n3) {
                void 0 === n3 && (n3 = r4), e5[n3] = t4[r4];
              }), i = this && this.__exportStar || function(e5, t4) {
                for (var r4 in e5) "default" === r4 || Object.prototype.hasOwnProperty.call(t4, r4) || n2(t4, e5, r4);
              };
              Object.defineProperty(t3, "__esModule", { value: true }), i(r3(8177), t3), i(r3(3012), t3), i(r3(4293), t3), i(r3(4478), t3), i(r3(6889), t3);
            }, 4478: function(e4, t3, r3) {
              "use strict";
              var n2 = this && this.__spreadArray || function(e5, t4, r4) {
                if (r4 || 2 === arguments.length) for (var n3, i2 = 0, o2 = t4.length; i2 < o2; i2++) !n3 && i2 in t4 || (n3 || (n3 = Array.prototype.slice.call(t4, 0, i2)), n3[i2] = t4[i2]);
                return e5.concat(n3 || Array.prototype.slice.call(t4));
              }, i = this && this.__importDefault || function(e5) {
                return e5 && e5.__esModule ? e5 : { default: e5 };
              };
              Object.defineProperty(t3, "__esModule", { value: true }), t3.mergeConfigsOld = t3.mergeConfigs = void 0;
              var o = i(r3(6924)), a = function(e5, t4) {
                if (Array.isArray(e5)) return t4;
              };
              t3.mergeConfigs = function(e5) {
                for (var t4 = [], r4 = 1; r4 < arguments.length; r4++) t4[r4 - 1] = arguments[r4];
                var i2 = t4.map(function(e6) {
                  return null == e6 || "object" != typeof e6;
                }).filter(Boolean).length > 0;
                if ("object" == typeof e5 && !i2) return o.default.apply(void 0, n2(n2([{}, e5], t4, false), [a], false));
              }, t3.mergeConfigsOld = function(e5) {
                for (var t4 = [], r4 = 1; r4 < arguments.length; r4++) t4[r4 - 1] = arguments[r4];
                return o.default.apply(void 0, n2(n2([{}, e5], t4, false), [function(e6, t5) {
                  if (Array.isArray(t5)) return t5;
                }], false));
              };
            }, 4293: function(e4, t3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.getErr = t3.getOk_or = t3.getOk = t3.isNotOk = t3.isOk = t3.err = t3.ok = void 0, t3.ok = function(e5) {
                return { ok: true, value: e5 };
              }, t3.err = function(e5) {
                return { ok: false, error: e5 };
              }, t3.isOk = function(e5) {
                return true === e5.ok;
              }, t3.isNotOk = function(e5) {
                return !(0, t3.isOk)(e5);
              }, t3.getOk = function(e5) {
                if (true === e5.ok) return e5.value;
              }, t3.getOk_or = function(e5, t4) {
                return true === e5.ok ? e5.value : t4;
              }, t3.getErr = function(e5) {
                if (true !== e5.ok) return e5.error;
              };
            }, 3012: function(e4, t3) {
              "use strict";
              Object.defineProperty(t3, "__esModule", { value: true }), t3.timedThrottle = void 0, t3.timedThrottle = function(e5, t4) {
                var r3 = 0;
                return function() {
                  for (var n2 = [], i = 0; i < arguments.length; i++) n2[i] = arguments[i];
                  var o = Date.now();
                  o - r3 >= t4 && (r3 = o, e5.apply(this, n2));
                };
              };
            } }, t2 = {};
            function r2(n2) {
              var i = t2[n2];
              if (void 0 !== i) return i.exports;
              var o = t2[n2] = { id: n2, loaded: false, exports: {} };
              return e3[n2].call(o.exports, o, o.exports, r2), o.loaded = true, o.exports;
            }
            return r2.g = (function() {
              if ("object" == typeof globalThis) return globalThis;
              try {
                return this || new Function("return this")();
              } catch (e4) {
                if ("object" == typeof window) return window;
              }
            })(), r2.nmd = function(e4) {
              return e4.paths = [], e4.children || (e4.children = []), e4;
            }, r2(8156);
          })();
        } }, t = {};
        function r(n2) {
          var i = t[n2];
          if (void 0 !== i) return i.exports;
          var o = t[n2] = { exports: {} };
          return e[n2].call(o.exports, o, o.exports, r), o.exports;
        }
        r.n = function(e2) {
          var t2 = e2 && e2.__esModule ? function() {
            return e2.default;
          } : function() {
            return e2;
          };
          return r.d(t2, { a: t2 }), t2;
        }, r.d = function(e2, t2) {
          for (var n2 in t2) r.o(t2, n2) && !r.o(e2, n2) && Object.defineProperty(e2, n2, { enumerable: true, get: t2[n2] });
        }, r.g = (function() {
          if ("object" == typeof globalThis) return globalThis;
          try {
            return this || new Function("return this")();
          } catch (e2) {
            if ("object" == typeof window) return window;
          }
        })(), r.o = function(e2, t2) {
          return Object.prototype.hasOwnProperty.call(e2, t2);
        }, r.r = function(e2) {
          "undefined" != typeof Symbol && Symbol.toStringTag && Object.defineProperty(e2, Symbol.toStringTag, { value: "Module" }), Object.defineProperty(e2, "__esModule", { value: true });
        };
        var n = {};
        !(function() {
          "use strict";
          r.r(n), r.d(n, { AdRollType: function() {
            return Ti;
          }, AuthorizationError: function() {
            return i;
          }, CaptureEventType: function() {
            return o;
          }, ErrorSource: function() {
            return e2;
          }, ErrorType: function() {
            return t2;
          }, LogLevel: function() {
            return Ve;
          }, MediaPlayer: function() {
            return Si;
          }, MetadataEventType: function() {
            return a;
          }, MetadataID3Type: function() {
            return s;
          }, PlayerEventType: function() {
            return u;
          }, PlayerState: function() {
            return c;
          }, RemotePlayerEvent: function() {
            return Qt;
          }, create: function() {
            return yi;
          }, createWorker: function() {
            return vi;
          }, getVersion: function() {
            return bi;
          }, isPlayerSupported: function() {
            return mi;
          }, isVP9Supported: function() {
            return m;
          }, isWasmSupported: function() {
            return gi;
          }, registerIVSQualityPlugin: function() {
            return Ii;
          }, registerIVSTech: function() {
            return Pi;
          } });
          var e2 = (function(e3) {
            return e3.UNKNOWN = "Unspecified", e3.FILE = "File", e3.SEGMENT = "Segment", e3.SOURCE = "Source", e3.DECODER = "Decode", e3.RENDERER = "Render", e3.MASTER_PLAYLIST = "MasterPlaylist", e3.MEDIA_PLAYLIST = "MediaPlaylist", e3.DRM = "DRM", e3;
          })({}), t2 = (function(e3) {
            return e3.GENERIC = "Error", e3.NOT_SUPPORTED = "ErrorNotSupported", e3.NO_SOURCE = "ErrorNoSource", e3.INVALID_DATA = "ErrorInvalidData", e3.INVALID_STATE = "ErrorInvalidState", e3.INVALID_PARAMETER = "ErrorInvalidParameter", e3.TIMEOUT = "ErrorTimeout", e3.NETWORK = "ErrorNetwork", e3.NETWORK_IO = "ErrorNetworkIO", e3.AUTHORIZATION = "ErrorAuthorization", e3.NOT_AVAILABLE = "ErrorNotAvailable", e3;
          })({}), i = (function(e3) {
            return e3[e3.GEOBLOCKED = 1] = "GEOBLOCKED", e3[e3.UNSUPPORTED_DEVICE = 2] = "UNSUPPORTED_DEVICE", e3[e3.ANONYMIZER_BLOCKED = 3] = "ANONYMIZER_BLOCKED", e3[e3.CELLULAR_NETWORK_PROHIBITED = 4] = "CELLULAR_NETWORK_PROHIBITED", e3[e3.UNAUTHORIZATION_ENTITLEMENTS = 5] = "UNAUTHORIZATION_ENTITLEMENTS", e3[e3.VOD_RESTRICTED = 6] = "VOD_RESTRICTED", e3;
          })({}), o = (function(e3) {
            return e3.CAPTURE_ENABLED = "CaptureEnabled", e3.CAPTURE_BUNDLE = "CaptureBundle", e3.CAPTURE_ANALYTICS = "CaptureAnalytics", e3.FMP4_SEGMENT = "FMP4Segment", e3;
          })({}), a = (function(e3) {
            return e3.ID3 = "MetaID3", e3.CAPTION = "MetaCaption", e3.SEI = "SEI", e3;
          })({}), s = (function(e3) {
            return e3.METADATA_ID = "metadata.live-video.net", e3.INBAND_METADATA_ID = "inband.metadata.live-video.net", e3;
          })({}), u = (function(e3) {
            return e3.INITIALIZED = "PlayerInitialized", e3.QUALITY_CHANGED = "PlayerQualityChanged", e3.QUALITIES_CHANGED = "PlayerQualitiesChanged", e3.DURATION_CHANGED = "PlayerDurationChanged", e3.VOLUME_CHANGED = "PlayerVolumeChanged", e3.MUTED_CHANGED = "PlayerMutedChanged", e3.PLAYBACK_RATE_CHANGED = "PlayerPlaybackRateChanged", e3.REBUFFERING = "PlayerRebuffering", e3.AUDIO_BLOCKED = "PlayerAudioBlocked", e3.PLAYBACK_BLOCKED = "PlayerPlaybackBlocked", e3.ERROR = "PlayerError", e3.RECOVERABLE_ERROR = "PlayerRecoverableError", e3.ANALYTICS_EVENT = "PlayerAnalyticsEvent", e3.TIME_UPDATE = "PlayerTimeUpdate", e3.SYNC_TIME_UPDATE = "PlayerSyncTimeUpdate", e3.BUFFER_UPDATE = "PlayerBufferUpdate", e3.SEEK_COMPLETED = "PlayerSeekCompleted", e3.SESSION_DATA = "PlayerSessionData", e3.STATE_CHANGED = "PlayerStateChanged", e3.WORKER_ERROR = "PlayerWorkerError", e3.METADATA = "PlayerMetadata", e3.TEXT_CUE = "PlayerTextCue", e3.TEXT_METADATA_CUE = "PlayerTextMetadataCue", e3.AD_CUE = "PlayerAdCue", e3.AD_BREAK_STARTED = "PlayerAdBreakStarted", e3.AD_CREATIVE_STARTED = "PlayerAdCreativeStarted", e3.AD_TIME_UPDATE = "PlayerAdTimeUpdate", e3.AD_CREATIVE_ENDED = "PlayerAdCreativeEnded", e3.AD_BREAK_ENDED = "PlayerAdBreakEnded", e3.STREAM_SOURCE_CUE = "PlayerStreamSourceCue", e3.NETWORK_UNAVAILABLE = "PlayerNetworkUnavailable", e3.SEGMENT_DISCONTINUITY = "PlayerSegmentDiscontinuity", e3.SEGMENT_METADATA = "PlayerSegmentMetadata", e3.SOURCE_GROUP_CHANGED = "PlayerSourceGroupChanged", e3.TEXT_TRACKS_CHANGED = "PlayerTextTracksChanged", e3.TEXT_TRACK_CHANGED = "PlayerTextTrackChanged", e3;
          })({}), c = (function(e3) {
            return e3.IDLE = "Idle", e3.READY = "Ready", e3.BUFFERING = "Buffering", e3.PLAYING = "Playing", e3.ENDED = "Ended", e3;
          })({}), l = require_es5(), d = require_extends(), f = r.n(d), h = require_createClass(), p = r.n(h);
          function v() {
            return "undefined" != typeof MediaSource;
          }
          function g(e3) {
            return "undefined" != typeof ManagedMediaSource && true === (null == e3 ? void 0 : e3.preferManagedMediaSource);
          }
          function m() {
            var e3 = xe();
            return "Windows" === e3.osName && (e3.chrome || e3.firefox || e3.msEdgeChromium) && v() ? "mediaCapabilities" in navigator ? navigator.mediaCapabilities.decodingInfo({ type: "media-source", video: { contentType: 'video/mp4;codecs="vp09.00.41.08"', width: 1920, height: 1080, bitrate: 8e6, framerate: 60 } }).then(function(e4) {
              return e4.supported && e4.smooth;
            }) : Promise.resolve(MediaSource.isTypeSupported('video/mp4;codecs="vp09.00.10.08"')) : Promise.resolve(false);
          }
          var y = 101, b = 0.1, E = 1 << 30, S = 12e4, T = 3e3, _ = { audio: 1936684398, video: 1986618469 }, C = (function() {
            function e3() {
              this.buffer = void 0, this.head = void 0, this.tail = void 0, this.buffer = [], this.head = 0, this.tail = 0;
            }
            var t3 = e3.prototype;
            return t3.push = function(e4) {
              this.tail === this.buffer.length ? this.buffer.push(e4) : this.buffer[this.tail] = e4, this.tail++;
            }, t3.pop = function() {
              var e4, t4 = null != (e4 = this.buffer[this.head]) ? e4 : null;
              return this.buffer[this.head] = null, this.head++, this.empty() && (this.head = 0, this.tail = 0), t4;
            }, t3.size = function() {
              return this.tail - this.head;
            }, t3.empty = function() {
              return this.head >= this.tail;
            }, e3;
          })();
          function k(e3) {
            try {
              return JSON.parse(e3);
            } catch (t3) {
              return console.error("Failed JSON parse:", e3), {};
            }
          }
          function w(e3, t3) {
            void 0 === t3 && (t3 = false);
            try {
              return t3 ? JSON.stringify(e3, void 0, 2) : JSON.stringify(e3);
            } catch (e4) {
              return "";
            }
          }
          function P(e3) {
            return "" === e3.codecs || "undefined" == typeof MediaSource || MediaSource.isTypeSupported('video/mp4;codecs="' + e3.codecs + '"');
          }
          function A(e3) {
            var t3, r2;
            return void 0 !== e3.hidden ? (t3 = "hidden", r2 = "visibilitychange") : void 0 !== e3.msHidden ? (t3 = "msHidden", r2 = "msvisibilitychange") : void 0 !== e3.webkitHidden && (t3 = "webkitHidden", r2 = "webkitvisibilitychange"), { hidden: t3, visibilityChange: r2 };
          }
          function I(e3, t3, r2) {
            return Math.min(r2, Math.max(t3, e3));
          }
          function D(e3) {
            e3.removeAttribute("src");
          }
          function x(e3) {
            if ("function" == typeof e3.checkVisibility) return e3.checkVisibility({ visibilityProperty: true });
            var t3 = e3.style;
            return "none" !== t3.display && "hidden" !== t3.visibility;
          }
          function M(e3) {
            return 0 | e3;
          }
          function R(e3) {
            return "number" == typeof e3.webkitDecodedFrameCount ? e3.webkitDecodedFrameCount : "function" == typeof e3.getVideoPlaybackQuality ? e3.getVideoPlaybackQuality().totalVideoFrames : "number" == typeof e3.mozDecodedFrames ? e3.mozDecodedFrames : 0;
          }
          function L(e3) {
            return "number" == typeof e3.webkitDroppedFrameCount ? e3.webkitDroppedFrameCount : "function" == typeof e3.getVideoPlaybackQuality ? e3.getVideoPlaybackQuality().droppedVideoFrames : 0;
          }
          function O(e3, t3) {
            for (var r2 = 0; r2 < t3.length; r2++) console.info(e3, "start: ", t3.start(r2), ", end: ", t3.end(r2));
          }
          function N(e3, t3, r2) {
            for (var n2 = 0; n2 < e3.length; n2++) {
              var i2 = e3.start(n2), o2 = e3.end(n2);
              if (!(o2 <= t3)) {
                if (i2 - r2 > t3) break;
                for (var a2 = n2 + 1; a2 < e3.length && !(e3.start(a2) - o2 > r2); a2++) o2 = e3.end(a2);
                for (var s2 = n2 - 1; s2 >= 0 && !(i2 - e3.end(s2) > r2); s2--) i2 = e3.start(s2);
                return { start: Math.min(i2, t3), end: o2 };
              }
            }
            return { start: t3, end: t3 };
          }
          function F(e3, t3, r2) {
            void 0 === r2 && (r2 = b);
            var n2 = N(e3, t3, r2).end - t3 > r2;
            if (e3.length > 1 || !n2) for (var i2 = 0; i2 < e3.length; i2++) {
              var o2 = e3.start(i2), a2 = e3.end(i2);
              if (t3 < o2 && a2 - o2 > r2) return o2 + r2;
            }
            return n2 ? t3 + r2 : t3;
          }
          function U(e3, t3, r2) {
            return e3.addEventListener(t3, r2), function() {
              e3.removeEventListener(t3, r2);
            };
          }
          function V(e3) {
            if (e3.src) {
              var t3 = e3.src;
              D(e3), e3.load(), URL.revokeObjectURL(t3);
            }
          }
          function B(e3) {
            for (var t3 = [], r2 = 0; r2 < e3.length; r2++) t3.push("[" + e3.start(r2) + ", " + e3.end(r2) + "]");
            return t3.join(",");
          }
          function G(e3) {
            for (var t3 = [], r2 = 0; r2 < e3.length; r2++) t3.push({ start: e3.start(r2), end: e3.end(r2) });
            return t3;
          }
          function j(e3) {
            var t3 = (e3 || "").match(/codecs="([^.]+)\./);
            return t3 && t3[1] ? t3[1] : "";
          }
          var H, W = (function() {
            function e3(e4, t4, r2, n2, i2) {
              this.rawCodec = t4, this.group = r2, this.isProtected = n2, this.onError = i2, this.pending = void 0, this.unsubscribers = [], this.srcBuf = void 0, this.blocked = false, this.srcBuf = e4, this.pending = new C(), this.unsubscribers.push(U(e4, "updateend", this.process.bind(this)));
            }
            var t3 = e3.prototype;
            return t3.getBufferedRanges = function() {
              try {
                return this.srcBuf ? G(this.srcBuf.buffered) : [];
              } catch (e4) {
                return [];
              }
            }, t3.abort = function() {
              this.schedule(function(e4) {
                e4.abort();
              });
            }, t3.changeType = function(e4) {
              this.rawCodec = (function(e5) {
                var t4 = (e5 || "").match(/codecs=".+"/);
                return t4 && t4[0] ? t4[0] : 'codecs=""';
              })(e4), this.schedule(function(t4) {
                t4.changeType(e4);
              });
            }, t3.appendBuffer = function(e4) {
              this.schedule(function(t4) {
                try {
                  t4.appendBuffer(e4);
                } catch (e5) {
                  if ("QuotaExceededError" !== e5.name) throw e5;
                  var r2 = t4.buffered, n2 = r2.start(0), i2 = r2.end(r2.length - 1), o2 = (n2 + i2) / 2;
                  t4.remove(o2, i2);
                }
              });
            }, t3.setTimestampOffset = function(e4) {
              this.schedule(function(t4) {
                t4.timestampOffset = e4;
              });
            }, t3.remove = function(e4, t4) {
              this.schedule(function(r2) {
                var n2 = r2.buffered;
                if (n2.length) {
                  var i2 = Math.max(e4, n2.start(0)), o2 = Math.min(t4, n2.end(n2.length - 1));
                  i2 < o2 && r2.remove(i2, o2);
                }
              });
            }, t3.block = function() {
              var e4 = this;
              return new Promise(function(t4) {
                e4.schedule(function() {
                  e4.blocked = true, t4();
                });
              });
            }, t3.unblock = function() {
              this.blocked = false, this.process();
            }, t3.destroy = function() {
              this.pending = new C(), this.unsubscribers.forEach(function(e4) {
                return e4();
              }), this.srcBuf = void 0;
            }, t3.schedule = function(e4) {
              this.pending.empty() && this.canProcess() ? this.safeExecute(e4) : (this.pending.push(e4), this.process());
            }, t3.safeExecute = function(e4) {
              try {
                if (!this.srcBuf) throw new Error("srcBuf is undefined");
                e4(this.srcBuf);
              } catch (e5) {
                this.onError(e5, false);
              }
            }, t3.process = function() {
              for (; !this.pending.empty() && this.canProcess(); ) this.safeExecute(this.pending.pop());
            }, t3.canProcess = function() {
              return !(!this.srcBuf || this.srcBuf.updating || this.blocked);
            }, p()(e3, [{ key: "buffer", get: function() {
              return this.srcBuf;
            } }, { key: "codec", get: function() {
              return this.rawCodec;
            } }, { key: "timestampOffset", get: function() {
              return this.buffer ? this.buffer.timestampOffset : 0;
            } }]);
          })(), K = r(223), z = function(e3) {
            var t3 = (0, K.createLogger)({ name: e3 });
            return { logger: t3, configureLogger: t3.getChildLogger({ name: "configure", category: "sink.configure" }), rebuildLogger: t3.getChildLogger({ name: "rebuild", category: "sink.rebuild" }) };
          }, q = z("mse-sink"), Q = q.logger, Y = q.configureLogger, Z = (function() {
            function e3(e4, t4, r2, n2) {
              this.mediaSource = e4, this.onEnded = t4, this.onError = r2, this.mediaSourceInfo = n2, this.expectedTracks = -1, this.supportsChangeType = true, this.pendingSourceBufferData = [], this.sourceBuffers = /* @__PURE__ */ Object.create(null), this.unsubscribers = [], this.config = { maxPendingSamples: 1024 }, this.unsubscribers.push(U(e4, "sourceended", this.onEnded));
            }
            e3.isSupported = function() {
              return void 0 !== self.MediaSource;
            }, e3.isSupportedInWorker = function() {
              return e3.isSupported() && MediaSource.canConstructInDedicatedWorker && "function" == typeof MediaSourceHandle;
            }, e3.create = function(t4, r2, n2) {
              var i2, o2 = false;
              g(n2) && ManagedMediaSource ? (Q.debug("using ManagedMediaSource"), o2 = true, i2 = new ManagedMediaSource()) : i2 = new MediaSource();
              var a2 = new Promise(function(n3, a3) {
                var s2 = U(i2, "sourceopen", function() {
                  "open" === i2.readyState ? (n3(new e3(i2, t4, r2, { isManagedMediaSource: o2 })), s2()) : a3("The MediaSource was closed upon opening");
                });
              });
              return { ms: i2, sink: a2 };
            };
            var t3 = e3.prototype;
            return t3.getMediaSourceInfo = function() {
              return f()({}, this.mediaSourceInfo);
            }, t3.getConfig = function() {
              return f()({}, this.config);
            }, t3.updateConfig = function(e4) {
              this.config = f()({}, this.config, e4);
            }, t3.getBufferedRanges = function(e4) {
              var t4, r2;
              return null != (t4 = null == (r2 = this.sourceBuffers[_[e4]]) ? void 0 : r2.getBufferedRanges()) ? t4 : [];
            }, t3.setExpectedTracks = function(e4) {
              this.expectedTracks = e4;
            }, t3.getExpectedTracks = function() {
              return this.expectedTracks;
            }, t3.setSupportsChangeType = function(e4) {
              this.supportsChangeType = e4;
            }, t3.getSupportsChangeType = function() {
              return this.supportsChangeType;
            }, t3.addTrack = function(e4, t4, r2, n2) {
              var i2 = this.mediaSource, o2 = this.sourceBuffers, a2 = (function(e5) {
                return "video/mp4;" + e5;
              })(t4);
              if (o2[e4]) {
                var s2, u2 = j(o2[e4].codec);
                return j(t4) !== u2 && o2[e4].changeType(a2), this.checkHaveAllExpectedTracks(), null != (s2 = o2[e4].buffer) ? s2 : null;
              }
              try {
                var c2 = i2.addSourceBuffer(a2);
                return o2[e4] = new W(c2, t4, r2, n2, this.handleError.bind(this)), this.checkHaveAllExpectedTracks(), c2;
              } catch (e5) {
                this.handleError(e5, "open" === i2.readyState);
              }
              return null;
            }, t3.append = function(e4, t4) {
              var r2, n2 = this.bufferProperties, i2 = this.expectedTracks, o2 = this.sourceBuffers, a2 = this.pendingSourceBufferData;
              n2.length < i2 ? (a2.push([e4, t4]), a2.length > this.config.maxPendingSamples && this.onError(102, y, "possible missing track", false)) : null == (r2 = o2[e4]) || r2.appendBuffer(t4);
            }, t3.remove = function(e4, t4) {
              for (var r2 = this.sourceBuffers, n2 = 0, i2 = Object.keys(r2); n2 < i2.length; n2++) r2[i2[n2]].remove(e4, t4);
            }, t3.setTimestampOffset = function(e4, t4) {
              var r2 = this.sourceBuffers[e4];
              r2 && (r2.abort(), r2.setTimestampOffset(t4));
            }, t3.setDuration = function(e4) {
              var t4 = this;
              this.scheduleUpdate(function() {
                return t4.mediaSource.duration = e4;
              }).catch(function(e5) {
                return t4.handleError(e5, false);
              });
            }, t3.updateDuration = function(e4, t4) {
              var r2 = (function(e5, t5, r3) {
                var n2 = e5;
                return e5 === 1 / 0 || e5 === E ? r3 ? n2 = 1 / 0 : r3 || (n2 = E) : e5 !== t5 && (n2 = e5), n2;
              })(e4, this.duration, t4);
              r2 !== this.duration && this.setDuration(r2);
            }, t3.setLiveSeekableRange = function(e4, t4) {
              var r2 = this;
              this.scheduleUpdate(function() {
                return r2.mediaSource.setLiveSeekableRange(e4, t4);
              }).catch(function(e5) {
                return r2.handleError(e5, false);
              });
            }, t3.scheduleUpdate = function(e4) {
              var t4 = this;
              void 0 === e4 && (e4 = X);
              var r2 = Object.keys(this.sourceBuffers).map(function(e5) {
                return t4.sourceBuffers[e5];
              });
              return Promise.all(r2.map(function(e5) {
                return e5.block();
              })).then(e4).then(function() {
                return r2.forEach(function(e5) {
                  return e5.unblock();
                });
              });
            }, t3.isDrmProtected = function() {
              return this.bufferProperties.some(function(e4) {
                return e4.isProtected;
              });
            }, t3.isAudioOnly = function() {
              return 1 === this.bufferProperties.length && "audio_only" === this.bufferProperties[0].group;
            }, t3.isTrackCompatible = function(e4) {
              var t4 = false === (false === this.supportsChangeType && this.isChangingCodecId(e4) || this.isChangingContentProtection(e4) || this.isChangingNumberOfTracks(e4.expectedTracks) || this.isChangingNumberOfCodecsInSingleTrack(e4, e4.expectedTracks));
              return Y.debug("isTrackCompatible", { isCompatible: t4 }), t4;
            }, t3.destroy = function() {
              this.destroySourceBuffers(), this.unsubscribers.forEach(function(e4) {
                return e4();
              }), this.unsubscribers = [];
            }, t3.isChangingContentProtection = function(e4) {
              var t4 = this.isDrmProtected(), r2 = t4 !== e4.isProtected;
              return Y.debug("isChangingContentProtection", { isChanging: r2, current: t4, next: e4.isProtected }), r2;
            }, t3.isChangingNumberOfTracks = function(e4) {
              var t4 = this.expectedTracks !== e4;
              return Y.debug("isChangingNumberOfTracks", { isChanging: t4, current: this.expectedTracks, next: e4 }), t4;
            }, t3.isChangingNumberOfCodecsInSingleTrack = function(e4, t4) {
              if (1 !== this.expectedTracks || 1 !== t4) return false;
              var r2 = e4.codec.split(",").length, n2 = this.bufferProperties[0].codec.split(",").length, i2 = r2 !== n2;
              return Y.debug("isChangingNumberOfCodecsInSingleTrack", { isChanging: i2, currentCodecCount: n2, nextCodecCount: r2 }), i2;
            }, t3.isChangingCodecId = function(e4) {
              var t4 = this.bufferProperties.find(function(t5) {
                return t5.trackID === e4.trackID;
              });
              if (!t4) return false;
              var r2 = j(t4.codec), n2 = j(e4.codec), i2 = r2 !== n2;
              return Y.debug("isChangingCodecId", { isChanging: i2, currentCodecId: r2, nextCodecId: n2 }), i2;
            }, t3.checkHaveAllExpectedTracks = function() {
              var e4 = this.sourceBuffers, t4 = this.bufferProperties, r2 = this.expectedTracks, n2 = this.pendingSourceBufferData;
              if (t4.length >= r2 && n2.length) {
                for (var i2 = 0; i2 < n2.length; ++i2) {
                  var o2, a2 = n2[i2], s2 = a2[0], u2 = a2[1];
                  null == (o2 = e4[s2]) || o2.appendBuffer(u2);
                }
                this.pendingSourceBufferData = [];
              }
            }, t3.handleError = function(e4, t4) {
              var r2 = e4.code || 102, n2 = 102;
              "NotSupportedError" === e4.name && (n2 = r2 = 4), this.onError(n2, r2, e4.message, t4);
            }, t3.destroySourceBuffers = function() {
              for (var e4 = this.mediaSource; e4.sourceBuffers.length > 0; ) try {
                e4.removeSourceBuffer(e4.sourceBuffers[0]);
              } catch (e5) {
                this.handleError(e5, false);
                break;
              }
              for (var t4 = 0, r2 = Object.keys(this.sourceBuffers); t4 < r2.length; t4++) {
                var n2 = r2[t4];
                this.sourceBuffers[n2].destroy();
              }
              this.sourceBuffers = /* @__PURE__ */ Object.create(null);
            }, p()(e3, [{ key: "duration", get: function() {
              return this.mediaSource.duration;
            } }, { key: "bufferProperties", get: function() {
              var e4 = this, t4 = this.sourceBuffers, r2 = Object.keys(t4);
              return r2.map(function(n2) {
                var i2 = t4[n2];
                return { trackID: Number(n2), codec: i2.codec, mode: "mse", path: "", isProtected: i2.isProtected, group: i2.group, srcObj: null, expectedTracks: r2.length, duration: e4.duration, configurationDetails: { manifestDiscontinuityPresent: false, inSkippableAd: false, adCreativeTransition: false } };
              });
            } }]);
          })(), X = function() {
          }, J = "model", $ = "type", ee = "vendor", te = "console", re = "mobile", ne = "tablet", ie = "smarttv", oe = "wearable", ae = "embedded", se = "Amazon", ue = "Apple", ce = "ASUS", le = "Google", de = "Huawei", fe = "LG", he = "Microsoft", pe = "Motorola", ve = "Samsung", ge = "Sharp", me = "Sony", ye = "Xiaomi", be = "Zebra", Ee = function(e3, t3) {
            if ("string" == typeof e3) return e3 = e3.replace(/^\s\s*/, ""), void 0 === t3 ? e3 : e3.substring(0, 500);
          }, Se = function(e3, t3) {
            for (var r2, n2, i2, o2, a2, s2, u2 = 0; t3 && u2 < t3.length && !a2; ) {
              var c2 = t3[u2], l2 = t3[u2 + 1];
              for (r2 = n2 = 0; r2 < c2.length && !a2 && c2[r2]; ) {
                var d2 = c2[r2++];
                if (a2 = "function" == typeof d2.exec ? d2.exec(e3) : void 0) for (i2 = 0; i2 < l2.length; i2++) s2 = a2[++n2], "object" == typeof (o2 = l2[i2]) && o2.length > 0 ? 2 === o2.length ? "function" == typeof o2[1] ? this[o2[0]] = o2[1].call(this, s2) : this[o2[0]] = o2[1] : 3 === o2.length ? "function" != typeof o2[1] || o2[1].exec && o2[1].test ? this[o2[0]] = "string" == typeof s2 ? s2.replace(o2[1], o2[2]) : void 0 : this[o2[0]] = s2 ? o2[1].call(this, s2, o2[2]) : void 0 : 4 === o2.length && void 0 !== o2[0] && (this[o2[0]] = s2 ? o2[3].call(this, s2.replace(o2[1], o2[2])) : void 0) : this[o2] = s2 || void 0;
              }
              u2 += 2;
            }
          }, Te = { device: [[/\b(sch-i[89]0\d|shw-m380s|sm-[ptx]\w{2,4}|gt-[pn]\d{2,4}|sgh-t8[56]9|nexus 10)/i], [J, [ee, ve], [$, ne]], [/\b((?:s[cgp]h|gt|sm)-\w+|sc[g-]?[\d]+a?|galaxy nexus)/i, /samsung[- ]([-\w]+)/i, /sec-(sgh\w+)/i], [J, [ee, ve], [$, re]], [/(?:\/|\()(ip(?:hone|od)[\w, ]*)(?:\/|;)/i], [J, [ee, ue], [$, re]], [/\((ipad);[-\w),; ]+apple/i, /applecoremedia\/[\w.]+ \((ipad)/i, /\b(ipad)\d\d?,\d\d?[;\]].+ios/i], [J, [ee, ue], [$, ne]], [/(macintosh);/i], [J, [ee, ue]], [/\b(sh-?[altvz]?\d\d[a-ekm]?)/i], [J, [ee, ge], [$, re]], [/\b((?:ag[rs][23]?|bah2?|sht?|btv)-a?[lw]\d{2})\b(?!.+d\/s)/i], [J, [ee, de], [$, ne]], [/(?:huawei|honor)([-\w ]+)[;)]/i, /\b(nexus 6p|\w{2,4}e?-[atu]?[ln][\dx][012359c][adn]?)\b(?!.+d\/s)/i], [J, [ee, de], [$, re]], [/\b(poco[\w ]+|m2\d{3}j\d\d[a-z]{2})(?: bui|\))/i, /\b; (\w+) build\/hm\1/i, /\b(hm[-_ ]?note?[_ ]?(?:\d\w)?) bui/i, /\b(redmi[-_ ]?(?:note|k)?[\w_ ]+)(?: bui|\))/i, /oid[^)]+; (m?[12][0-389][01]\w{3,6}[c-y])( bui|; wv|\))/i, /\b(mi[-_ ]?(?:a\d|one|one[_ ]plus|note lte|max|cc)?[_ ]?(?:\d?\w?)[_ ]?(?:plus|se|lite)?)(?: bui|\))/i], [[J, /_/g, " "], [ee, ye], [$, re]], [/oid[^)]+; (2\d{4}(283|rpbf)[cgl])( bui|\))/i, /\b(mi[-_ ]?(?:pad)(?:[\w_ ]+))(?: bui|\))/i], [[J, /_/g, " "], [ee, ye], [$, ne]], [/; (\w+) bui.+ oppo/i, /\b(cph[12]\d{3}|p(?:af|c[al]|d\w|e[ar])[mt]\d0|x9007|a101op)\b/i], [J, [ee, "OPPO"], [$, re]], [/\b(opd2\d{3}a?) bui/i], [J, [ee, "OPPO"], [$, ne]], [/vivo (\w+)(?: bui|\))/i, /\b(v[12]\d{3}\w?[at])(?: bui|;)/i], [J, [ee, "Vivo"], [$, re]], [/\b(rmx[1-3]\d{3})(?: bui|;|\))/i], [J, [ee, "Realme"], [$, re]], [/\b(milestone|droid(?:[2-4x]| (?:bionic|x2|pro|razr))?:?( 4g)?)\b[\w ]+build\//i, /\bmot(?:orola)?[- ](\w*)/i, /((?:moto[\w() ]+|xt\d{3,4}|nexus 6)(?= bui|\)))/i], [J, [ee, pe], [$, re]], [/\b(mz60\d|xoom[2 ]{0,2}) build\//i], [J, [ee, pe], [$, ne]], [/((?=lg)?[vl]k-?\d{3}) bui| 3\.[-\w; ]{10}lg?-([06cv9]{3,4})/i], [J, [ee, fe], [$, ne]], [/(lm(?:-?f100[nv]?|-[\w.]+)(?= bui|\))|nexus [45])/i, /\blg[-e;/ ]+((?!browser|netcast|android tv)\w+)/i, /\blg-?([\d\w]+) bui/i], [J, [ee, fe], [$, re]], [/(ideatab[-\w ]+)/i, /lenovo ?(s[56]000[-\w]+|tab(?:[\w ]+)|yt[-\d\w]{6}|tb[-\d\w]{6})/i], [J, [ee, "Lenovo"], [$, ne]], [/(?:maemo|nokia).*(n900|lumia \d+)/i, /nokia[-_ ]?([-\w.]*)/i], [[J, /_/g, " "], [ee, "Nokia"], [$, re]], [/(pixel c)\b/i], [J, [ee, le], [$, ne]], [/droid.+; (pixel[\daxl ]{0,6})(?: bui|\))/i], [J, [ee, le], [$, re]], [/droid.+ (a?\d[0-2]{2}so|[c-g]\d{4}|so[-gl]\w+|xq-a\w[4-7][12])(?= bui|\).+chrome\/(?![1-6]{0,1}\d\.))/i], [J, [ee, me], [$, re]], [/sony tablet [ps]/i, /\b(?:sony)?sgp\w+(?: bui|\))/i], [[J, "Xperia Tablet"], [ee, me], [$, ne]], [/ (kb2005|in20[12]5|be20[12][59])\b/i, /(?:one)?(?:plus)? (a\d0\d\d)(?: b|\))/i], [J, [ee, "OnePlus"], [$, re]], [/(alexa)webm/i, /(kf[a-z]{2}wi|aeo[c-r]{2})( bui|\))/i, /(kf[a-z]+)( bui|\)).+silk\//i], [J, [ee, se], [$, ne]], [/((?:sd|kf)[0349hijorstuw]+)( bui|\)).+silk\//i], [[J, /(.+)/g, "Fire Phone $1"], [ee, se], [$, re]], [/(playbook);[-\w),; ]+(rim)/i], [J, ee, [$, ne]], [/\b((?:bb[a-f]|st[hv])100-\d)/i, /\(bb10; (\w+)/i], [J, [ee, "BlackBerry"], [$, re]], [/(?:\b|asus_)(transfo[prime ]{4,10} \w+|eeepc|slider \w+|nexus 7|padfone|p00[cj])/i], [J, [ee, ce], [$, ne]], [/ (z[bes]6[027][012][km][ls]|zenfone \d\w?)\b/i], [J, [ee, ce], [$, re]], [/(nexus 9)/i], [J, [ee, "HTC"], [$, ne]], [/(htc)[-;_ ]{1,2}([\w ]+(?=\)| bui)|\w+)/i, /(zte)[- ]([\w ]+?)(?: bui|\/|\))/i, /(alcatel|geeksphone|nexian|panasonic(?!(?:;|\.))|sony(?!-bra))[-_ ]?([-\w]*)/i], [ee, [J, /_/g, " "], [$, re]], [/droid.+; ([ab][1-7]-?[0178a]\d\d?)/i], [J, [ee, "Acer"], [$, ne]], [/droid.+; (m[1-5] note) bui/i, /\bmz-([-\w]{2,})/i], [J, [ee, "Meizu"], [$, re]], [/; ((?:power )?armor(?:[\w ]{0,8}))(?: bui|\))/i], [J, [ee, "Ulefone"], [$, re]], [/(blackberry|benq|palm(?=-)|sonyericsson|acer|asus|dell|meizu|motorola|polytron|infinix|tecno)[-_ ]?([-\w]*)/i, /(hp) ([\w ]+\w)/i, /(asus)-?(\w+)/i, /(microsoft); (lumia[\w ]+)/i, /(lenovo)[-_ ]?([-\w]+)/i, /(jolla)/i, /(oppo) ?([\w ]+) bui/i], [ee, J, [$, re]], [/(kobo)\s(ereader|touch)/i, /(archos) (gamepad2?)/i, /(hp).+(touchpad(?!.+tablet)|tablet)/i, /(kindle)\/([\w.]+)/i, /(nook)[\w ]+build\/(\w+)/i, /(dell) (strea[kpr\d ]*[\dko])/i, /(le[- ]+pan)[- ]+(\w{1,9}) bui/i, /(trinity)[- ]*(t\d{3}) bui/i, /(gigaset)[- ]+(q\w{1,9}) bui/i, /(vodafone) ([\w ]+)(?:\)| bui)/i], [ee, J, [$, ne]], [/(surface duo)/i], [J, [ee, he], [$, ne]], [/droid [\d.]+; (fp\du?)(?: b|\))/i], [J, [ee, "Fairphone"], [$, re]], [/(u304aa)/i], [J, [ee, "AT&T"], [$, re]], [/\bsie-(\w*)/i], [J, [ee, "Siemens"], [$, re]], [/\b(rct\w+) b/i], [J, [ee, "RCA"], [$, ne]], [/\b(venue[\d ]{2,7}) b/i], [J, [ee, "Dell"], [$, ne]], [/\b(q(?:mv|ta)\w+) b/i], [J, [ee, "Verizon"], [$, ne]], [/\b(?:barnes[& ]+noble |bn[rt])([\w+ ]*) b/i], [J, [ee, "Barnes & Noble"], [$, ne]], [/\b(tm\d{3}\w+) b/i], [J, [ee, "NuVision"], [$, ne]], [/\b(k88) b/i], [J, [ee, "ZTE"], [$, ne]], [/\b(nx\d{3}j) b/i], [J, [ee, "ZTE"], [$, re]], [/\b(gen\d{3}) b.+49h/i], [J, [ee, "Swiss"], [$, re]], [/\b(zur\d{3}) b/i], [J, [ee, "Swiss"], [$, ne]], [/\b((zeki)?tb.*\b) b/i], [J, [ee, "Zeki"], [$, ne]], [/\b([yr]\d{2}) b/i, /\b(dragon[- ]+touch |dt)(\w{5}) b/i], [[ee, "Dragon Touch"], J, [$, ne]], [/\b(ns-?\w{0,9}) b/i], [J, [ee, "Insignia"], [$, ne]], [/\b((nxa|next)-?\w{0,9}) b/i], [J, [ee, "NextBook"], [$, ne]], [/\b(xtreme_)?(v(1[045]|2[015]|[3469]0|7[05])) b/i], [[ee, "Voice"], J, [$, re]], [/\b(lvtel-)?(v1[12]) b/i], [[ee, "LvTel"], J, [$, re]], [/\b(ph-1) /i], [J, [ee, "Essential"], [$, re]], [/\b(v(100md|700na|7011|917g).*\b) b/i], [J, [ee, "Envizen"], [$, ne]], [/\b(trio[-\w. ]+) b/i], [J, [ee, "MachSpeed"], [$, ne]], [/\btu_(1491) b/i], [J, [ee, "Rotor"], [$, ne]], [/(shield[\w ]+) b/i], [J, [ee, "Nvidia"], [$, ne]], [/(sprint) (\w+)/i], [ee, J, [$, re]], [/(kin\.[onetw]{3})/i], [[J, /\./g, " "], [ee, he], [$, re]], [/droid.+; (cc6666?|et5[16]|mc[239][23]x?|vc8[03]x?)\)/i], [J, [ee, be], [$, ne]], [/droid.+; (ec30|ps20|tc[2-8]\d[kx])\)/i], [J, [ee, be], [$, re]], [/smart-tv.+(samsung)/i], [ee, [$, ie]], [/hbbtv.+maple;(\d+)/i], [[J, /^/, "SmartTV"], [ee, ve], [$, ie]], [/(nux; netcast.+smarttv|lg (netcast\.tv-201\d|android tv))/i], [[ee, fe], [$, ie]], [/(apple) ?tv/i], [ee, [J, ue + " TV"], [$, ie]], [/crkey/i], [[J, "Chromecast"], [ee, le], [$, ie]], [/droid.+aft(\w+)( bui|\))/i], [J, [ee, se], [$, ie]], [/\(dtv[);].+(aquos)/i, /(aquos-tv[\w ]+)\)/i], [J, [ee, ge], [$, ie]], [/(bravia[\w ]+)( bui|\))/i], [J, [ee, me], [$, ie]], [/(mitv-\w{5}) bui/i], [J, [ee, ye], [$, ie]], [/Hbbtv.*(technisat) (.*);/i], [ee, J, [$, ie]], [/\b(roku)[\dx]*[)/]((?:dvp-)?[\d.]*)/i, /hbbtv\/\d+\.\d+\.\d+ +\([\w+ ]*; *([\w\d][^;]*);([^;]*)/i], [[ee, Ee], [J, Ee], [$, ie]], [/\b(android tv|smart[- ]?tv|opera tv|tv; rv:)\b/i], [[$, ie]], [/(ouya)/i, /(nintendo) ([wids3utch]+)/i], [ee, J, [$, te]], [/droid.+; (shield) bui/i], [J, [ee, "Nvidia"], [$, te]], [/(playstation [345portablevi]+)/i], [J, [ee, me], [$, te]], [/\b(xbox(?: one)?(?!; xbox))[); ]/i], [J, [ee, he], [$, te]], [/((pebble))app/i], [ee, J, [$, oe]], [/(watch)(?: ?os[,/]|\d,\d\/)[\d.]+/i], [J, [ee, ue], [$, oe]], [/droid.+; (glass) \d/i], [J, [ee, le], [$, oe]], [/droid.+; (wt63?0{2,3})\)/i], [J, [ee, be], [$, oe]], [/(quest( \d| pro)?)/i], [J, [ee, "Facebook"], [$, oe]], [/(tesla)(?: qtcarbrowser|\/[-\w.]+)/i], [ee, [$, ae]], [/(aeobc)\b/i], [J, [ee, se], [$, ae]], [/droid .+?; ([^;]+?)(?: bui|; wv\)|\) applew).+? mobile safari/i], [J, [$, re]], [/droid .+?; ([^;]+?)(?: bui|\) applew).+?(?! mobile) safari/i], [J, [$, ne]], [/\b((tablet|tab)[;/]|focus\/\d(?!.+mobile))/i], [[$, ne]], [/(phone|mobile(?:[;/]| [ \w/.]*safari)|pda(?=.+windows ce))/i], [[$, re]], [/(android[-\w. ]{0,9});.+buil/i], [J, [ee, "Generic"]]] }, _e = function() {
            var e3, t3 = {};
            return t3[ee] = void 0, t3[J] = void 0, t3[$] = void 0, Se.call(t3, null != (e3 = navigator.userAgent) ? e3 : "", Te.device), t3;
          }, Ce = (function(e3) {
            return e3.DESKTOP = "web", e3.MOBILE_WEB = "mobile_web", e3.FIRETV = "firetv_web_tv", e3.XBOX = "xbox_web_tv", e3.PS5 = "ps5_web_tv", e3.TIZEN = "samsung_web_tv", e3.WEBOS = "lg_web_tv", e3.PS4 = "ps4_web_tv", e3.ANDROIDTV = "androidtv_web_tv", e3.VIZIO = "vizio_web_tv", e3.SWITCH = "switch_web_tv", e3.VESTEL = "vestel_web_tv", e3.WEBTV = "web_tv", e3.CHROMECAST = "chromecast", e3.OTHERTV = "other_web_tv", e3;
          })({}), ke = (function(e3) {
            return e3.CHROME = "chrome", e3.EDGE = "msEdgeChromium", e3.FIREFOX = "firefox", e3.OPERA = "opera", e3.SAFARI = "safari", e3;
          })({}), we = /^(\d+)\.(\d+)\.(\d+)[+|-]?(.*)?$/, Pe = /^(\d+)\.(\d+)[+|-]?(.*)?$/, Ae = /^(\d+)$/, Ie = ((H = { platform: Ce.DESKTOP })[ke.CHROME] = false, H[ke.EDGE] = false, H[ke.FIREFOX] = false, H[ke.OPERA] = false, H[ke.SAFARI] = false, H.chromecast = false, H.domain = "", H.electron = false, H.manufacturer = "", H.model = "", H.family = "", H.host = "", H.major = -1, H.minor = -1, H.msEdgeLegacy = false, H.msIE = false, H.name = "", H.osName = "", H.osVersion = "", H.patch = -1, H.url = "", H.userAgent = "", H.mobile = false, H.supportsDataChannels = false, H.supportsWebTransport = false, H.supportsMSEInWorkers = false, H.supportsMuxedFMP4 = true, H), De = null;
          function xe() {
            var e3, t3, r2;
            if (De) return De;
            if ("undefined" == typeof window || "undefined" == typeof navigator) return De = Ie;
            var n2, i2, o2 = navigator.userAgent, a2 = l.getParser(o2), s2 = (n2 = String(a2.getBrowserVersion()), i2 = we.exec(n2) || Pe.exec(n2) || Ae.exec(n2) || [], { major: parseInt(i2[1], 10) || 0, minor: parseInt(i2[2], 10) || 0, patch: parseInt(i2[3], 10) || 0 }), u2 = a2.getEngine(), c2 = a2.getOSName(), d2 = String(a2.getOSVersion()), f2 = !(!a2.some(["mobile"]) && !a2.some(["tablet"])), h2 = _e();
            return (r2 = { platform: Re(o2, a2, f2) })[ke.CHROME] = !!a2.some([ke.CHROME]), r2[ke.FIREFOX] = !!a2.some([ke.FIREFOX]), r2[ke.EDGE] = !!a2.some(["microsoft edge"]) && "Blink" === u2.name, r2[ke.OPERA] = !!a2.some([ke.OPERA]), r2[ke.SAFARI] = !!a2.some([ke.SAFARI]), r2.chromecast = navigator.userAgent.toLowerCase().indexOf("crkey") > -1, r2.domain = window.location.host.split(".").slice(-2).join("."), r2.electron = !!a2.some(["electron"]), r2.manufacturer = null != (e3 = h2.vendor) ? e3 : "", r2.model = null != (t3 = h2.model) ? t3 : "", r2.family = a2.getBrowserName().toLowerCase(), r2.host = window.location.host, r2.major = s2.major, r2.minor = s2.minor, r2.msEdgeLegacy = !!a2.some(["microsoft edge"]) && "Blink" !== u2.name, r2.msIE = !!a2.some(["internet explorer"]), r2.name = navigator.appVersion, r2.osName = c2, r2.osVersion = d2, r2.patch = s2.patch, r2.url = window.location.href, r2.userAgent = navigator.userAgent, r2.mobile = f2, r2.supportsDataChannels = "RTCPeerConnection" in window, r2.supportsWebTransport = "WebTransport" in window, r2.supportsMSEInWorkers = Z.isSupportedInWorker(), r2.supportsMuxedFMP4 = false === navigator.userAgent.toLowerCase().includes("playstation"), Me(De = r2), De;
          }
          var Me = function(e3) {
            "microsoft edge" === e3.family && (e3.family = "edge"), "samsung internet for android" === e3.family && (e3.family = "samsung internet");
          };
          function Re(e3, t3, r2) {
            if ((function(e4) {
              return /AFT[A-Z0-9]{1,}/i.test(e4);
            })(e3)) return Ce.FIRETV;
            if ((function(e4) {
              return /Xbox/i.test(e4);
            })(e3)) return Ce.XBOX;
            if ((function(e4) {
              return /playstation\s5/i.test(e4);
            })(e3)) return Ce.PS5;
            if ((function(e4) {
              return /playstation\s4/i.test(e4);
            })(e3)) return Ce.PS4;
            var n2 = t3.getOSName();
            return /tizen/i.test(n2) ? Ce.TIZEN : /webos/i.test(n2) ? Ce.WEBOS : (function(e4) {
              var t4 = /Android.*Build\/[A-Z0-9.]+/i.test(e4), r3 = /; wv\)/.test(e4);
              return !Le(e4) && t4 && r3;
            })(e3) ? Ce.ANDROIDTV : Le(e3) ? Ce.CHROMECAST : r2 ? Ce.MOBILE_WEB : Ce.DESKTOP;
          }
          function Le(e3) {
            return /Chromecast/i.test(e3);
          }
          var Oe = require_asyncToGenerator(), Ne = r.n(Oe), Fe = require_regenerator2(), Ue = r.n(Fe), Ve = (function(e3) {
            return e3.DEBUG = "debug", e3.INFO = "info", e3.WARN = "warn", e3.ERROR = "error", e3;
          })({}), Be = (0, K.createLogger)({ name: "VideoTransformer", enabled: true });
          Be.setConfigByLevel(Ve.WARN);
          var Ge, je, He = (function(e3) {
            return e3.gpu_pending = "Awaiting WebGPU adapter", e3.gpu_unavailable = "WebGPU unavailable", e3.gpu_adapter_unavailable = "WebGPU adapter unavailable", e3.gpu_adapterinfo_undefined = "WebGPU adapter.info undefined", e3.gpu_error = "WebGPU error", e3;
          })({}), We = (function(e3) {
            return e3.AVC = "avc1", e3.HEVC = "hev1", e3.AV1 = "av01", e3;
          })({}), Ke = require_mergeWith(), ze = r.n(Ke), qe = function(e3) {
            for (var t3 = arguments.length, r2 = new Array(t3 > 1 ? t3 - 1 : 0), n2 = 1; n2 < t3; n2++) r2[n2 - 1] = arguments[n2];
            return ze().apply(void 0, [{}, e3].concat(r2, [function(e4, t4) {
              if (Array.isArray(t4)) return t4;
            }]));
          }, Qe = "e32ac33fa53a3db6ed281b9223ca365d3311390344f591a48a8af30798b129ee", Ye = { rebuildMediaSinkOnSourceQualityChange: false, rebuildMediaSinkOnDiscontinuity: false, abrTranscodesOnly: false }, Ze = { adaptiveBitrate: { useProbeEndpoint: false, useScore: false }, media: { supportsCodecProfileTransition: true, codecConfigs: [], supportsMixedCodec: true, readers: { mp4: {} } }, logLevel: Ve.WARN, logCategories: [], features: { gpu: { flags: { enable_render_surface: false, add_canvas_to_surface: false, allow_canvas_visible: false, init_transformer: false, run_transformer: false }, render: {}, module: {}, configured: false }, mseInWorkers: { enable: true, supportedBrowsers: (Ge = {}, Ge[ke.CHROME] = true, Ge[ke.EDGE] = false, Ge[ke.FIREFOX] = false, Ge[ke.SAFARI] = false, Ge[ke.OPERA] = false, Ge) }, supportsMuxedFMP4: true, useAdLoudnessMetadata: true, sink: {}, optimizeBackgroundPlayback: { enabled: false, switchDelayMs: 6e4 }, allowBackgroundControl: false, subtitles: { enableHlsSubtitlePlaylists: false } }, analytics: { sendTwitchEvents: false, additionalEventProperties: {}, overrideEndpointUrlWithSessionData: true, useSeparateMinuteWatchedEventForTwitchClips: false }, experiments: {}, network: { hlsParallelRequests: false, edgePrewarm: false } }, Xe = qe(Ze, { adaptiveBitrate: { useProbeEndpoint: true, useScore: true }, analytics: { sendTwitchEvents: true, additionalEventProperties: {}, overrideEndpointUrlWithSessionData: true, useSeparateMinuteWatchedEventForTwitchClips: true } }), Je = qe(Xe, { media: { readers: { mp4: { samplesPerFlush: 40 } } } }), $e = qe(Xe, { features: { supportsMuxedFMP4: false } }), et = function(e3) {
            return function(t3) {
              return e3(t3) || null === t3;
            };
          }, tt = { string: function(e3) {
            return "string" == typeof e3;
          }, boolean: function(e3) {
            return "boolean" == typeof e3;
          }, number: function(e3) {
            return "number" == typeof e3;
          }, record: function(e3) {
            return null != e3 && "object" == typeof e3;
          } }, rt = function(e3) {
            for (var t3 = arguments.length, r2 = new Array(t3 > 1 ? t3 - 1 : 0), n2 = 1; n2 < t3; n2++) r2[n2 - 1] = arguments[n2];
            var i2 = Object.assign.apply(Object, [{}].concat(r2)), o2 = {};
            for (var a2 in e3) a2 in i2 && e3[a2](i2[a2]) && (o2[a2] = i2[a2]);
            return o2;
          }, nt = { channel_id: tt.string, client_app: tt.string, content_mode: tt.string, location: tt.string, player: tt.string, staff: tt.boolean, user_id: tt.string, gpu_supported: tt.boolean, gpu_unsupported_reason: tt.string, gpu_architecture: tt.string, gpu_description: tt.string, gpu_device: tt.string, gpu_vendor: tt.string, gl_renderer: et(tt.string), gl_vendor: et(tt.string), battery_percent: (je = tt.number, function(e3) {
            return je(e3) || void 0 === e3;
          }) }, it = function() {
            for (var e3 = arguments.length, t3 = new Array(e3), r2 = 0; r2 < e3; r2++) t3[r2] = arguments[r2];
            return rt.apply(void 0, [nt].concat(t3));
          }, ot = { mediaType: tt.string, metadata: tt.record }, at = function(e3, t3) {
            var r2, n2;
            void 0 === t3 && (t3 = {});
            var i2 = rt(ot, e3);
            return { mediaType: null != (r2 = i2.mediaType) ? r2 : "", metadata: it(null != (n2 = i2.metadata) ? n2 : {}, t3) };
          }, st = { droppedFrameFilterThresholdCoefficient: { type: "number", required: false }, useScore: { type: "boolean", required: false }, useProbeEndpoint: { type: "boolean", required: false } }, ut = { codecConfigs: { type: "array", required: false, items: { type: "object", required: false, config: { schema: { codecString: { type: "string", required: true }, setting: { type: "object", required: true, config: { schema: { skipPlatformSupportChecks: { type: "boolean", required: false }, disableUse: { type: "boolean", required: false } } } } } } } }, supportsMixedCodec: { type: "boolean", required: false }, readers: { type: "object", required: false, config: { schema: { mp4: { type: "object", required: false, config: { schema: { samplesPerFlush: { type: "number", required: false } } } } } } }, preferManagedMediaSource: { type: "boolean", required: false }, supportsCodecProfileTransition: { type: "boolean", required: true } }, ct = { edgePrewarm: { type: "boolean", required: false }, hlsParallelRequests: { type: "boolean", required: false } }, lt = { enableHlsSubtitlePlaylists: { type: "boolean", required: true } }, dt = (function() {
            function e3(e4) {
              this.emitAnalytics = void 0, this.emitAnalytics = e4;
            }
            var t3 = e3.prototype;
            return t3.onValue = function(e4) {
              this.emit("ivs_devconf_value", e4);
            }, t3.onError = function(e4) {
              this.emit("ivs_devconf_error", e4);
            }, t3.onTrace = function(e4) {
              this.emit("ivs_devconf_trace", e4);
            }, t3.onAssignment = function(e4) {
              this.emit("ivs_devconf_rollout_assignment", e4);
            }, t3.emit = function(e4, t4) {
              this.emitAnalytics(e4, t4);
            }, e3;
          })(), ft = ["experiment_foobar"], ht = (function() {
            function e3(e4, t4, r2) {
              var n2;
              void 0 === t4 && (t4 = {}), this.externalContext = e4, this.onConfigChanged = r2, this.logger = void 0, this.defaultConfig = void 0, this.mainConfig = void 0, this.deviceConfigManager = void 0, this.deviceConfigAnalyticsHandler = void 0, this.playSessionConfig = {}, this.context = void 0, this.context = f()({}, e4, { cFilter: (n2 = t4.metadata, void 0 === n2 ? "4810125360c1f23cffe72c51023d2105a9a2a94c40641d3b9311e0befb0b5963" : Qe) }), this.logger = (0, K.createLogger)({ name: "config-manager" }), this.defaultConfig = (function(e5, t5) {
                var r3 = Ze;
                return t5 === Qe && (r3 = e5.platform === Ce.WEBOS ? Je : e5.platform === Ce.PS5 ? $e : Xe), (function(e6, t6) {
                  return null != e6 && e6.safari ? t6.media.supportsCodecProfileTransition = false : t6.media.supportsCodecProfileTransition = true, t6;
                })(e5, r3);
              })(this.context.browserContext, this.context.cFilter), this.mainConfig = qe(this.defaultConfig, pt(t4)), this.logger.debug("Initialized"), this.logger.debug("context " + w(this.context, true)), this.logger.debug("initConfig " + w(t4, true)), this.logger.debug("defaultConfig " + w(this.defaultConfig, true)), this.logger.debug("mainConfig " + w(this.mainConfig, true));
            }
            var t3 = e3.prototype;
            return t3.initDeviceConfigManager = function(e4) {
              var t4, r2;
              if (!this.deviceConfigManager) {
                var n2 = null != (t4 = null == (r2 = this.context.deviceConfig) ? void 0 : r2.env) ? t4 : K.DeviceConfigEnv.PROD;
                this.deviceConfigManager = K.DeviceConfigManager.getInstance(window, { fileKey: "player-web-v1", standardEnv: n2, analyticsProperties: { client_sdk: "player-web", env: n2 }, refresh: { canRefreshNow: e4.canRefreshNow }, emitMetrics: e4.emitMetrics, enableConsoleLog: this.logger.canLog(Ve.DEBUG) }), this.deviceConfigManager && (this.deviceConfigAnalyticsHandler = new dt(e4.emitAnalytics));
              }
            }, t3.updateConfigFromDeviceConfig = function() {
              var e4, t4, r2 = this.getDeviceConfigPropertyHolder();
              if (!r2) return this.logger.warn("Attempted to update from Device Config, but failed to get the property holder"), false;
              this.logger.debug("Acquired property holder");
              var n2 = f()({}, this.context.browserContext, { cFilter: this.context.cFilter, sdkVersion: this.context.sdkVersion }), i2 = null != (e4 = null == (t4 = this.context.deviceConfig) ? void 0 : t4.trace) && e4, o2 = this.getExperiments(r2, n2, i2);
              this.logger.debug("experiments " + w(o2, true));
              var a2 = f()({}, o2, n2), s2 = r2.getResolvedConfigForPropertyKey({ key: "adaptiveBitrate", context: a2, schema: st, trace: i2 }), u2 = r2.getResolvedConfigForPropertyKey({ key: "media", context: a2, schema: ut, trace: i2 }), c2 = r2.getResolvedConfigForPropertyKey({ key: "network", context: a2, schema: ct, trace: i2 }), l2 = r2.getResolvedConfigForPropertyKey({ key: "subtitles", context: a2, schema: lt, trace: i2 });
              return this.updateMainConfig({ adaptiveBitrate: s2, media: u2, network: c2, features: { subtitles: l2 } });
            }, t3.getDeviceConfigPropertyHolder = function() {
              if (this.deviceConfigManager && this.deviceConfigAnalyticsHandler) return this.deviceConfigManager.getConfigurationHolder({ analytics: this.deviceConfigAnalyticsHandler });
            }, t3.resetLoadConfig = function(e4) {
              this.updatePlaySessionConfig({ analytics: { additionalEventProperties: e4.metadata } });
            }, t3.updateLoadConfig = function(e4) {
              this.updatePlaySessionConfig(qe(this.playSessionConfig, { analytics: { additionalEventProperties: e4.metadata } }));
            }, t3.getConfigSnapshot = function() {
              return qe(this.mainConfig, this.playSessionConfig);
            }, t3.setExperiment = function(e4) {
              var t4 = e4.id;
              this.mainConfig.experiments[t4] = e4;
            }, t3.setLogLevel = function(e4) {
              this.updateMainConfig({ logLevel: e4 });
            }, t3.delete = function() {
              this.resetMainConfig();
            }, t3.getExperiments = function(e4, t4, r2) {
              for (var n2 = e4.getResolvedExperimentForPropertyKeys({ keys: [].concat(ft), context: t4, trace: r2 }), i2 = {}, o2 = 0, a2 = ft; o2 < a2.length; o2++) {
                var s2 = a2[o2], u2 = n2[s2];
                null != u2 && u2.variant && (i2[s2] = u2.variant);
              }
              return i2;
            }, t3.resetMainConfig = function() {
              this.mainConfig = f()({}, this.defaultConfig);
            }, t3.updateMainConfig = function(e4) {
              var t4 = f()({}, this.mainConfig);
              this.mainConfig = qe(t4, e4);
              var r2 = this.notifyIfConfigChanged(t4, this.mainConfig);
              return r2 && this.logger.debug("mainConfig changed " + w(this.mainConfig, true)), r2;
            }, t3.updatePlaySessionConfig = function(e4) {
              var t4 = f()({}, this.playSessionConfig);
              this.playSessionConfig = e4, this.notifyIfConfigChanged(t4, this.playSessionConfig) && this.logger.debug("Session config changed " + w(this.playSessionConfig, true));
            }, t3.notifyIfConfigChanged = function(e4, t4) {
              return r2 = t4, w(e4) !== w(r2) && (this.onConfigChanged(), true);
              var r2;
            }, e3;
          })(), pt = function(e3) {
            var t3, r2;
            return { media: e3.mediaConfig, features: { optimizeBackgroundPlayback: e3.optimizeBackgroundPlayback, supportsMuxedFMP4: e3.supportsMuxedFMP4, allowBackgroundControl: e3.allowBackgroundControl, subtitles: { enableHlsSubtitlePlaylists: null == (t3 = e3.subtitles) ? void 0 : t3.enableHlsSubtitlePlaylists } }, logLevel: e3.logLevel, logCategories: e3.logCategories, analytics: { additionalEventProperties: it(null != (r2 = e3.metadata) ? r2 : {}) } };
          }, vt = (function() {
            function e3() {
              this.emitter = new K.TypedEmitter({}), this.queue = new Array();
            }
            e3.getInstance = function() {
              return e3.gInstance || (e3.gInstance = new e3()), e3.gInstance;
            };
            var t3 = e3.prototype;
            return t3.registerMetricsCallback = function(e4) {
              this.emitter.on("newMetrics", e4);
            }, t3.unregisterMetricsCallback = function(e4) {
              this.emitter.off("newMetrics", e4);
            }, t3.enqueue = function(e4) {
              this.queue.push(e4), this.emitter.emit("newMetrics");
            }, t3.dequeueAll = function() {
              var e4 = this.queue;
              return this.queue = new Array(), e4;
            }, e3;
          })();
          vt.gInstance = void 0;
          var gt = { keySystem: "org.w3.clearkey", uuid: "1077efec-c0b2-4d02-ace3-3c1e52e2fb4b" }, mt = { keySystem: "com.apple.fps.2_0", certUrl: "https://fairplay.twitch.keyos.com/api/v4/getCertificate?certHash=a17fd33d3843df9b17679ccf50a419b2", licenseUrl: "https://fairplay.twitch.keyos.com/api/v4/getLicense", uuid: "94CE86FB-07FF-4F43-ADB8-93D2FA968CA2" }, yt = { keySystem: "com.microsoft.playready", licenseUrl: "https://playready.twitch.keyos.com/api/v4/getLicense", uuid: "9a04f079-9840-4286-ab92-e65be0885f95" }, bt = { keySystem: "com.widevine.alpha", licenseUrl: "https://widevine.twitch.keyos.com/api/v4/getLicense", uuid: "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed" }, Et = { CLEARKEY: gt, FAIRPLAY: mt, PLAYREADY: yt, WIDEVINE: bt }, St = { "com.widevine.alpha": bt, "com.microsoft.playready": yt, "com.apple.fps.2_0": mt, "org.w3.clearkey": gt }, Tt = { value: 4, message: "Your browser does not support any DRM Content Decryption Modules" }, _t = { value: 4, message: "There was an issue while updating DRM License" }, Ct = { value: 204, message: "Error while requesting DRM license" }, kt = { value: 201, message: "DRM license not authorized for this browser version" }, wt = { value: 202, message: "DRM license not available" }, Pt = { value: 203, message: "DRM license server error" }, At = { value: 4, message: "Error creating key session" }, It = { value: 4, message: "Encryption key not usable because of internal error in CDM" }, Dt = { value: 4, message: "Unable to find valid CDM support on media" }, xt = { value: 2, message: "Request for AuthXML failed" }, Mt = { value: 2, message: "Request for DRM certificate failed" }, Rt = { value: 4, message: "Failed to parse DRM content data" }, Lt = { value: 4, message: "No initialization data available for DRM session" };
          function Ot(e3) {
            return window.WebKitMediaKeys && "function" == typeof window.WebKitMediaKeys.isTypeSupported && window.WebKitMediaKeys.isTypeSupported(Et.FAIRPLAY.keySystem) ? Et.FAIRPLAY.uuid : "function" == typeof navigator.requestMediaKeySystemAccess ? e3.safari ? "" : e3.msIE || e3.msEdgeLegacy ? Et.PLAYREADY.uuid : Et.WIDEVINE.uuid : "";
          }
          function Nt(e3, t3) {
            if ((e3 = Ft(e3)) === (t3 = Ft(t3))) return true;
            if (e3.byteLength !== t3.byteLength) return false;
            for (var r2 = new DataView(e3), n2 = new DataView(t3), i2 = 0; i2 < r2.byteLength; i2++) if (r2.getUint8(i2) !== n2.getUint8(i2)) return false;
            return true;
          }
          function Ft(e3) {
            return e3 instanceof Uint8Array || e3 instanceof Uint16Array ? e3.buffer : e3;
          }
          function Ut(e3) {
            return t3 = (function(e4) {
              if (null === e4) return [];
              for (var t4 = new DataView(e4.buffer || e4), r3 = [], n2 = 0; !(n2 >= t4.buffer.byteLength); ) {
                var i2 = n2 + t4.getUint32(n2);
                if (n2 += 4, t4.getUint32(n2) === Gt("pssh")) {
                  n2 += 4;
                  var o2 = t4.getUint8(n2);
                  if (0 === o2 || 1 === o2) {
                    n2++, n2 += 3;
                    for (var a2 = "", s2 = 0; s2 < 4; s2++) a2 += Ht(t4.getUint8(n2 + s2));
                    n2 += 4, a2 += "-";
                    for (var u2 = 0; u2 < 2; u2++) a2 += Ht(t4.getUint8(n2 + u2));
                    n2 += 2, a2 += "-";
                    for (var c2 = 0; c2 < 2; c2++) a2 += Ht(t4.getUint8(n2 + c2));
                    n2 += 2, a2 += "-";
                    for (var l2 = 0; l2 < 2; l2++) a2 += Ht(t4.getUint8(n2 + l2));
                    n2 += 2, a2 += "-";
                    for (var d2 = 0; d2 < 6; d2++) a2 += Ht(t4.getUint8(n2 + d2));
                    n2 += 6, a2 = a2.toLowerCase(), n2 += 4, r3.push(a2), n2 = i2;
                  } else n2 = i2;
                } else n2 = i2;
              }
              return r3;
            })(e3), r2 = [], t3.forEach(function(e4) {
              Object.keys(Et).forEach(function(t4) {
                var n2 = Et[t4];
                n2.uuid.toLowerCase() === e4.toLowerCase() && r2.push(n2);
              });
            }), r2;
            var t3, r2;
          }
          function Vt(e3, t3) {
            return new Promise(function(r2, n2) {
              var i2 = new XMLHttpRequest();
              for (var o2 in i2.open(t3.method, e3, true), t3.headers) Object.prototype.hasOwnProperty.call(t3.headers, o2) && i2.setRequestHeader(o2, t3.headers[o2]);
              i2.responseType = t3.responseType, i2.onload = function() {
                200 === i2.status && r2(i2.response);
              }, i2.onloadend = function() {
                n2(i2.status);
              }, i2.send(t3.body);
            });
          }
          function Bt(e3, t3) {
            for (var r2 = new DataView(e3.buffer, e3.byteOffset, e3.byteLength), n2 = Gt(t3), i2 = 0; i2 < e3.byteLength; ) {
              var o2 = r2.getUint32(i2);
              if (r2.getUint32(i2 + 4) === n2) return (0, K.ok)(e3.subarray(i2 + 8, i2 + o2));
              i2 += o2;
            }
            return (0, K.err)("Box type '" + t3 + "' not found");
          }
          function Gt(e3) {
            return e3.charCodeAt(0) << 24 | e3.charCodeAt(1) << 16 | e3.charCodeAt(2) << 8 | e3.charCodeAt(3);
          }
          function jt(e3) {
            return Array.from(e3).map(function(e4) {
              return e4.toString(16).padStart(2, "0");
            }).join("");
          }
          function Ht(e3) {
            var t3 = e3.toString(16);
            return 1 === t3.length ? "0" + t3 : t3;
          }
          function Wt(e3) {
            try {
              for (var t3 = atob(e3), r2 = t3.length, n2 = new Uint8Array(r2), i2 = 0; i2 < r2; i2++) n2[i2] = t3.charCodeAt(i2);
              return (0, K.ok)(n2);
            } catch (e4) {
              return (0, K.err)("Failed to decode base64: " + e4);
            }
          }
          function Kt(e3) {
            try {
              return (0, K.ok)(btoa(String.fromCharCode.apply(null, new Uint16Array(e3))));
            } catch (e4) {
              return (0, K.err)("Failed to encode base64: " + e4);
            }
          }
          function zt(e3) {
            return decodeURIComponent(e3.replace(/\+/g, " "));
          }
          function qt(e3, t3) {
            try {
              for (var r2 = new Uint8Array(e3), n2 = "", i2 = 0; i2 < r2.length; i2 += 8192) {
                var o2 = r2.subarray(i2, Math.min(i2 + 8192, r2.length));
                n2 += String.fromCharCode.apply(null, Array.from(o2));
              }
              if ("skd" === t3) return n2.startsWith("skd://") ? (0, K.ok)(n2.substring(6)) : (0, K.err)("Failed to parse FairPlay init data: missing skd:// scheme prefix");
              var a2 = Wt(JSON.parse(n2).sinf[0]);
              if (!a2.ok) return (0, K.err)("Failed to decode sinf: " + a2.error);
              var s2 = Bt(a2.value, "schi");
              if (!s2.ok) return (0, K.err)("Failed to find schi box: " + s2.error);
              var u2 = Bt(s2.value, "tenc");
              if (!u2.ok) return (0, K.err)("Failed to find tenc box: " + u2.error);
              var c2 = jt(u2.value.subarray(8, 24));
              return (0, K.ok)(c2);
            } catch (e4) {
              var l2 = "Failed to parse FairPlay init data: " + (e4 instanceof Error ? e4.message : String(e4));
              return (0, K.err)(l2);
            }
          }
          var Qt = (function(e3) {
            return e3.AVAILABLE = "RemotePlayerAvailable", e3.UNAVAILABLE = "RemotePlayerUnavailable", e3.SESSION_STARTED = "RemotePlayerSessionStarted", e3.SESSION_ENDED = "RemotePlayerSessionEnded", e3;
          })({}), Yt = (require_readOnlyError(), Math.ceil(240) + 1), Zt = function(e3, t3) {
            var r2 = [{ frames: e3, timestamp: t3 }];
            return { update: function(e4, t4) {
              if (!(t4 <= r2[r2.length - 1].timestamp)) {
                r2.push({ frames: e4, timestamp: t4 });
                for (var n2 = t4 - 4e3; r2.length > 2 && r2[1].timestamp <= n2; ) r2.shift();
                r2.length > Yt && r2.shift();
              }
            }, framerate: function() {
              if (r2.length < 2) return 0;
              var e4 = r2[0], t4 = r2[r2.length - 1], n2 = t4.timestamp - e4.timestamp;
              return n2 <= 0 ? 0 : (t4.frames - e4.frames) / n2 * 1e3;
            } };
          }, Xt = function(e3) {
            return e3.setAttribute("playsinline", ""), { element: function() {
              return e3;
            }, displayDimensions: function() {
              return { width: e3.clientWidth, height: e3.clientHeight };
            }, videoDimensions: function() {
              return { width: e3.videoWidth, height: e3.videoHeight };
            }, stats: function() {
              return { timestamp: performance.now(), droppedFrames: L(e3), decodedFrames: R(e3) };
            }, visible: function() {
              return x(e3);
            } };
          }, Jt = (function(e3) {
            return e3.video = "video", e3.canvas = "canvas", e3.debug_both = "debug_both", e3;
          })({}), $t = (function() {
            function e3(e4) {
              this.context = e4, this.video = void 0, this.canvas = void 0, this.surfaceContainer = void 0, this.framerateTracker = void 0, this.surfaceContainer = e4.createElement("div");
              var t4 = e4.createElement("video");
              this.video = Xt(t4), this.moveVideoToRenderSurface();
              var r2 = this.video.stats();
              this.framerateTracker = Zt(r2.decodedFrames, r2.timestamp);
            }
            var t3 = e3.prototype;
            return t3.setVideo = function(e4) {
              if (this.video.element() !== e4) {
                this.activeSurface() !== Jt.video && this.hideSurface(e4);
                var t4 = Xt(e4), r2 = t4.stats();
                this.framerateTracker = Zt(r2.decodedFrames, r2.timestamp), this.video = t4;
              }
            }, t3.moveVideoToRenderSurface = function() {
              this.replaceElement(Jt.video, this.video.element());
            }, t3.setCanvas = function(e4) {
              this.canvas !== e4 && (this.activeSurface() !== Jt.canvas && this.hideSurface(e4.element()), this.replaceElement(Jt.canvas, e4.element()), this.canvas = e4);
            }, t3.setActiveSurface = function(e4) {
              if (e4 !== this.activeSurface()) {
                if (e4 === Jt.canvas) {
                  if (!this.canvas) return void console.warn("[RenderSurface]: Tried to set canvas surface to active, but the canvas surface is undefiend.");
                  this.hideSurface(this.video.element()), this.showSurface(this.canvas.element());
                } else if (e4 === Jt.video) {
                  var t4;
                  this.hideSurface(null == (t4 = this.canvas) ? void 0 : t4.element()), this.showSurface(this.video.element());
                } else if (e4 === Jt.debug_both) {
                  var r2;
                  this.showSurface(null == (r2 = this.canvas) ? void 0 : r2.element()), this.showSurface(this.video.element());
                }
              }
            }, t3.surface = function() {
              return this.surfaceContainer;
            }, t3.videoElement = function() {
              return this.video.element();
            }, t3.displayDimensions = function() {
              return this.video.displayDimensions();
            }, t3.videoDimensions = function() {
              return this.video.videoDimensions();
            }, t3.stats = function() {
              var e4 = this.video.stats();
              return this.framerateTracker.update(e4.decodedFrames, e4.timestamp), e4;
            }, t3.framerate = function() {
              return this.framerateTracker.framerate();
            }, t3.activeSurface = function() {
              var e4;
              return null != (e4 = this.canvas) && e4.visible() ? Jt.canvas : Jt.video;
            }, t3.safeAppendSurface = function(e4) {
              try {
                this.surfaceContainer.appendChild(e4);
              } catch (e5) {
                console.warn("[RenderSurface]: Failed to append surface to container", e5);
              }
            }, t3.safeRemoveSurface = function(e4) {
              try {
                var t4 = this.surfaceContainer.querySelector(e4);
                t4 && this.surfaceContainer.removeChild(t4);
              } catch (e5) {
                console.warn("[RenderSurface]: Failed to remove surface from container", e5);
              }
            }, t3.replaceElement = function(e4, t4) {
              this.safeRemoveSurface(e4), this.safeAppendSurface(t4);
            }, t3.hideSurface = function(e4) {
              e4 && (e4.style.visibility = "hidden", e4.style.position = "absolute", e4.style.inset = "0");
            }, t3.showSurface = function(e4) {
              e4 && (e4.style.visibility = "inherit", e4.style.position = "inherit", e4.style.inset = "inherit");
            }, e3;
          })(), er = require_events(), tr = (function() {
            function e3() {
              this.emitter = void 0, this.emitter = new er.EventEmitter();
            }
            var t3 = e3.prototype;
            return t3.on = function(e4, t4) {
              this.emitter.on(String(e4), t4);
            }, t3.removeListener = function(e4, t4) {
              this.emitter.removeListener(String(e4), t4);
            }, t3.emit = function(e4) {
              for (var t4, r2 = arguments.length, n2 = new Array(r2 > 1 ? r2 - 1 : 0), i2 = 1; i2 < r2; i2++) n2[i2 - 1] = arguments[i2];
              (t4 = this.emitter).emit.apply(t4, [String(e4)].concat(n2));
            }, t3.removeAllListeners = function() {
              this.emitter.removeAllListeners();
            }, e3;
          })(), rr = (function(e3) {
            return e3.Pending = "pending", e3.Ready = "ready", e3.Configuring = "configuring", e3.Active = "active", e3.Error = "error", e3;
          })({}), nr = (function(e3) {
            return e3.Heartbeat = "heartbeat", e3.Configure = "configure", e3.Set_Log_Config = "setLogConfig", e3.Debug = "debug", e3.Destroy = "destroy", e3;
          })({}), ir = (function(e3) {
            return e3.State_Changed = "stateChanged", e3.Manifest_Load_Analytics = "manifestLoadAnalytics", e3.Available_Segments = "availableSegments", e3;
          })({}), or = (0, K.createLogger)({ name: "sw-host-config" });
          function ar(e3, t3) {
            var r2 = "undefined" != typeof Symbol && e3[Symbol.iterator] || e3["@@iterator"];
            if (r2) return (r2 = r2.call(e3)).next.bind(r2);
            if (Array.isArray(e3) || (r2 = (function(e4, t4) {
              if (e4) {
                if ("string" == typeof e4) return sr(e4, t4);
                var r3 = {}.toString.call(e4).slice(8, -1);
                return "Object" === r3 && e4.constructor && (r3 = e4.constructor.name), "Map" === r3 || "Set" === r3 ? Array.from(e4) : "Arguments" === r3 || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(r3) ? sr(e4, t4) : void 0;
              }
            })(e3)) || t3 && e3 && "number" == typeof e3.length) {
              r2 && (e3 = r2);
              var n2 = 0;
              return function() {
                return n2 >= e3.length ? { done: true } : { done: false, value: e3[n2++] };
              };
            }
            throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
          }
          function sr(e3, t3) {
            (null == t3 || t3 > e3.length) && (t3 = e3.length);
            for (var r2 = 0, n2 = Array(t3); r2 < t3; r2++) n2[r2] = e3[r2];
            return n2;
          }
          var ur, cr = (0, K.createLogger)({ name: "sw-host" }), lr = function() {
            return ur;
          }, dr = (function(e3) {
            return e3[e3.Pending = 0] = "Pending", e3[e3.Installing = 1] = "Installing", e3[e3.Ready = 2] = "Ready", e3[e3.Error = 3] = "Error", e3;
          })(dr || {}), fr = function(e3) {
            var t3, r2 = function() {
              return { isMSESupported: v() };
            };
            if (((t3 = e3) ? t3.url || (or.error("Service worker config must specify a url"), 0) : (or.error("Service worker config is required"), 0)) && (function(e4, t4) {
              return !(!t4.forceActivate && e4.isMSESupported);
            })(r2(), e3)) {
              var n2, i2, o2 = new tr(), a2 = true, s2 = dr.Pending, u2 = [], c2 = [], l2 = [], d2 = rr.Pending, h2 = false, p2 = (function() {
                var t4 = Ne()(Ue().mark(function t5() {
                  var r3, n3, i3, o3;
                  return Ue().wrap(function(t6) {
                    for (; ; ) switch (t6.prev = t6.next) {
                      case 0:
                        return t6.prev = 0, y2(dr.Installing), navigator.serviceWorker.addEventListener("message", E2), t6.prev = 1, t6.next = 2, g2(e3.url);
                      case 2:
                        t6.next = 4;
                        break;
                      case 3:
                        t6.prev = 3, i3 = t6.catch(1), cr.error("[registerAndActivate] Failed to unregister service worker instances: " + i3);
                      case 4:
                        return r3 = void 0 !== e3.scope ? { scope: e3.scope } : void 0, t6.next = 5, navigator.serviceWorker.register(e3.url, r3);
                      case 5:
                        return (n3 = t6.sent).installing ? cr.log("Service worker installing") : n3.waiting ? cr.log("Service worker installed") : n3.active && cr.log("Service worker active"), t6.next = 6, m2(n3);
                      case 6:
                        y2(dr.Ready), t6.next = 8;
                        break;
                      case 7:
                        t6.prev = 7, o3 = t6.catch(0), cr.error("Registration failed with " + o3), y2(dr.Error);
                      case 8:
                      case "end":
                        return t6.stop();
                    }
                  }, t5, null, [[0, 7], [1, 3]]);
                }));
                return function() {
                  return t4.apply(this, arguments);
                };
              })(), g2 = (function() {
                var e4 = Ne()(Ue().mark(function e5(t4) {
                  var r3;
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        return r3 = new RegExp(t4), e6.next = 1, navigator.serviceWorker.getRegistrations().then((function() {
                          var e7 = Ne()(Ue().mark(function e8(t5) {
                            var n3, i3, o3, a3;
                            return Ue().wrap(function(e9) {
                              for (; ; ) switch (e9.prev = e9.next) {
                                case 0:
                                  cr.debug("[unregisterOtherWorkerInstances] registrations", t5), n3 = ar(t5);
                                case 1:
                                  if ((i3 = n3()).done) {
                                    e9.next = 6;
                                    break;
                                  }
                                  if (null === (o3 = i3.value).active || !r3.test(o3.active.scriptURL)) {
                                    e9.next = 5;
                                    break;
                                  }
                                  return cr.debug("[unregisterOtherWorkerInstances] unregistering:", o3), e9.prev = 2, e9.next = 3, o3.unregister();
                                case 3:
                                  e9.next = 5;
                                  break;
                                case 4:
                                  e9.prev = 4, a3 = e9.catch(2), cr.error("[unregisterOtherWorkerInstances] Failed to unregister service worker: " + a3);
                                case 5:
                                  e9.next = 1;
                                  break;
                                case 6:
                                case "end":
                                  return e9.stop();
                              }
                            }, e8, null, [[2, 4]]);
                          }));
                          return function(t5) {
                            return e7.apply(this, arguments);
                          };
                        })());
                      case 1:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function(t4) {
                  return e4.apply(this, arguments);
                };
              })(), m2 = (function() {
                var e4 = Ne()(Ue().mark(function e5(t4) {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        return e6.abrupt("return", new Promise(function(e7, r3) {
                          null !== t4.installing || null !== t4.waiting ? (t4.installing || t4.waiting).addEventListener("statechange", function(t5) {
                            var n3 = t5.target;
                            cr.debug("sw registration, statechange", n3.state), "activated" === n3.state ? (cr.log("Service worker activated"), e7()) : "redundant" === n3.state && r3();
                          }) : e7();
                        }));
                      case 1:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function(t4) {
                  return e4.apply(this, arguments);
                };
              })(), y2 = function(e4) {
                if (cr.log("[changeHostState] host state changed from " + s2 + " to " + e4), (s2 = e4) === dr.Ready) {
                  var t4 = u2.slice();
                  u2 = [];
                  for (var r3, n3 = ar(t4); !(r3 = n3()).done; ) (0, r3.value)();
                  var i3 = c2.slice();
                  c2 = [];
                  for (var o3, a3 = ar(i3); !(o3 = a3()).done; ) {
                    var d3 = o3.value;
                    S2(d3);
                  }
                } else if (s2 === dr.Error) {
                  var f2 = l2.slice();
                  l2 = [];
                  for (var h3, p3 = ar(f2); !(h3 = p3()).done; ) (0, h3.value)();
                }
              }, b2 = function(e4) {
                s2 === dr.Error ? e4() : l2.push(e4);
              }, E2 = function(e4) {
                var t4 = e4.data;
                switch (t4.type) {
                  case ir.State_Changed:
                    var r3 = t4.state;
                    d2 === rr.Configuring && (h2 = false), r3 === rr.Ready && d2 !== rr.Pending && cr.warn("[handleMessage] worker was restarted by the browser"), d2 = r3;
                    break;
                  case ir.Manifest_Load_Analytics:
                    cr.debug("Manifest_Load_Analytics", t4);
                }
                o2.emit(t4.type, t4);
              }, S2 = (function() {
                var e4 = Ne()(Ue().mark(function e5(t4) {
                  var r3;
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        return cr.log("[sendMessage] sending message: ", t4), e6.abrupt("return", null == (r3 = navigator.serviceWorker.controller) ? void 0 : r3.postMessage(t4));
                      case 1:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function(t4) {
                  return e4.apply(this, arguments);
                };
              })(), T2 = (function() {
                var e4 = Ne()(Ue().mark(function e5(t4) {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        if (d2 !== rr.Pending) {
                          e6.next = 1;
                          break;
                        }
                        return cr.log("[sendMessageWhenReady] worker is not ready, queueing message", t4), c2.push(t4), e6.abrupt("return");
                      case 1:
                        S2(t4);
                      case 2:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function(t4) {
                  return e4.apply(this, arguments);
                };
              })(), _2 = function() {
                i2 && clearTimeout(i2), i2 = setTimeout(function() {
                  C2();
                }, 1e4);
              }, C2 = function() {
                a2 && d2 !== rr.Pending && (S2({ type: nr.Heartbeat }), _2());
              }, k2 = function(e4, t4) {
                var r3 = function(n3) {
                  n3.type === ir.State_Changed && n3.state === e4 && (o2.removeListener(ir.State_Changed, r3), t4(n3));
                };
                o2.on(ir.State_Changed, r3);
              }, w2 = (function() {
                var e4 = Ne()(Ue().mark(function e5() {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        if (s2 !== dr.Ready) {
                          e6.next = 1;
                          break;
                        }
                        return e6.abrupt("return");
                      case 1:
                        return e6.abrupt("return", new Promise(function(e7, t4) {
                          var r3;
                          r3 = function() {
                            cr.log("[ensureHostReady] host state changed to ready"), e7();
                          }, s2 === dr.Ready ? r3() : u2.push(r3), b2(function() {
                            cr.log("[ensureHostReady] host state changed to error"), t4();
                          });
                        }));
                      case 2:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function() {
                  return e4.apply(this, arguments);
                };
              })(), P2 = (function() {
                var e4 = Ne()(Ue().mark(function e5() {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        if (d2 === rr.Pending) {
                          e6.next = 1;
                          break;
                        }
                        return e6.abrupt("return");
                      case 1:
                        return e6.abrupt("return", new Promise(function(e7) {
                          k2(rr.Ready, function(t4) {
                            cr.log("[ensureWorkerReady] worker state changed to ready", t4), e7();
                          });
                        }));
                      case 2:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function() {
                  return e4.apply(this, arguments);
                };
              })(), A2 = (function() {
                var e4 = Ne()(Ue().mark(function e5() {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        return e6.next = 1, w2();
                      case 1:
                        return e6.next = 2, P2();
                      case 2:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function() {
                  return e4.apply(this, arguments);
                };
              })(), I2 = (function() {
                var e4 = Ne()(Ue().mark(function e5(t4) {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        return h2 = true, n2 = D2(null != t4 ? t4 : {}), e6.next = 1, A2();
                      case 1:
                        S2({ type: nr.Configure, config: n2 });
                      case 2:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function(t4) {
                  return e4.apply(this, arguments);
                };
              })(), D2 = function(e4) {
                return f()({}, n2, e4);
              }, x2 = (function() {
                var e4 = Ne()(Ue().mark(function e5() {
                  return Ue().wrap(function(e6) {
                    for (; ; ) switch (e6.prev = e6.next) {
                      case 0:
                        if (h2 || d2 !== rr.Active) {
                          e6.next = 1;
                          break;
                        }
                        return e6.abrupt("return");
                      case 1:
                        return e6.next = 2, A2();
                      case 2:
                        return e6.abrupt("return", new Promise(function(e7) {
                          k2(rr.Active, function(t4) {
                            cr.log("[ensureConfigured] worker state changed to active", t4), e7();
                          });
                        }));
                      case 3:
                      case "end":
                        return e6.stop();
                    }
                  }, e5);
                }));
                return function() {
                  return e4.apply(this, arguments);
                };
              })();
              return (function() {
                var t4, i3, o3, a3, s3, u3, c3, l3, d3, h3, p3, v2, g3, m3, y3, b3, E3, T3, C3, k3, w3 = r2().isMSESupported ? { cacheMultivariant: false, transformTargetDuration: false, lowLatencyMode: { enabled: false, useLocalSegment: false, convertPrefetchSegment: false, preloadPrefetchSegments: false }, driftDetection: { enabled: false, maxPlaylistPosition: 0, triggerDetectionThreshold: 0, minDurationBetweenCorrections: 0 }, debug: { addMultivariantDelay: void 0, addFirstVariantDelay: void 0 } } : { cacheMultivariant: true, transformTargetDuration: false, lowLatencyMode: { enabled: true, useLocalSegment: true, convertPrefetchSegment: true, preloadPrefetchSegments: false }, driftDetection: { enabled: true, maxPlaylistPosition: 3, triggerDetectionThreshold: 10, minDurationBetweenCorrections: 60 }, debug: { addMultivariantDelay: void 0, addFirstVariantDelay: void 0 } };
                n2 = { cacheMultivariant: null != (t4 = e3.cacheMultivariant) ? t4 : w3.cacheMultivariant, transformTargetDuration: null != (i3 = e3.transformTargetDuration) ? i3 : w3.transformTargetDuration, lowLatencyMode: { enabled: null != (o3 = null == (a3 = e3.lowLatencyMode) ? void 0 : a3.enabled) ? o3 : w3.lowLatencyMode.enabled, useLocalSegment: null != (s3 = null == (u3 = e3.lowLatencyMode) ? void 0 : u3.useLocalSegment) ? s3 : w3.lowLatencyMode.useLocalSegment, convertPrefetchSegment: null != (c3 = null == (l3 = e3.lowLatencyMode) ? void 0 : l3.convertPrefetchSegment) ? c3 : w3.lowLatencyMode.convertPrefetchSegment, preloadPrefetchSegments: null != (d3 = null == (h3 = e3.lowLatencyMode) ? void 0 : h3.preloadPrefetchSegments) ? d3 : w3.lowLatencyMode.preloadPrefetchSegments }, driftDetection: { enabled: null != (p3 = null == (v2 = e3.driftDetection) ? void 0 : v2.enabled) ? p3 : w3.driftDetection.enabled, maxPlaylistPosition: null != (g3 = null == (m3 = e3.driftDetection) ? void 0 : m3.maxPlaylistPosition) ? g3 : w3.driftDetection.maxPlaylistPosition, triggerDetectionThreshold: null != (y3 = null == (b3 = e3.driftDetection) ? void 0 : b3.triggerDetectionThreshold) ? y3 : w3.driftDetection.triggerDetectionThreshold, minDurationBetweenCorrections: null != (E3 = null == (T3 = e3.driftDetection) ? void 0 : T3.minDurationBetweenCorrections) ? E3 : w3.driftDetection.minDurationBetweenCorrections }, debug: { addMultivariantDelay: null == (C3 = e3.debug) ? void 0 : C3.addMultivariantDelay, addFirstVariantDelay: null == (k3 = e3.debug) ? void 0 : k3.addFirstVariantDelay } }, cr.info("[init] instantiating with config", n2), void 0 !== e3.debug && (e3.debug.api = { delayNextMediaManifest: function(e4) {
                  !(function(e5) {
                    if ("delayNextMediaManifest" === e5.api) {
                      var t5 = e5.delayInMs;
                      cr.log("[handleDebugApiMessage] delaying next manifest by " + t5 + "ms");
                    }
                    S2(f()({ type: nr.Debug }, e5));
                  })({ api: "delayNextMediaManifest", delayInMs: e4 });
                } }), _2();
              })(), { registerAndActivate: p2, addListener: function(e4, t4) {
                o2.on(e4, t4);
              }, removeListener: function(e4, t4) {
                o2.removeListener(e4, t4);
              }, sendMessage: S2, ensureReady: A2, configure: I2, ensureConfigured: x2, getConfig: function() {
                return n2;
              }, getDriftDetectionConfig: function() {
                var e4;
                return null == (e4 = n2) ? void 0 : e4.driftDetection;
              }, setLogConfig: function(e4) {
                T2({ type: nr.Set_Log_Config, config: e4 });
              }, destroy: function() {
                cr.log("[destroy] destroying"), a2 = false, i2 && clearTimeout(i2), o2.removeAllListeners(), navigator.serviceWorker.removeEventListener("message", E2), S2({ type: nr.Destroy });
              }, debugPassthroughAnalytics: function(t4) {
                var r3;
                null == (r3 = e3.debug) || null == r3.onPassthroughAnalytics || r3.onPassthroughAnalytics(t4);
              } };
            }
          };
          function hr(e3) {
            var t3 = e3.querySelector("Challenge"), r2 = null == t3 ? void 0 : t3.textContent;
            return r2 ? atob(r2) : "";
          }
          function pr(e3) {
            var t3 = e3.querySelectorAll("HttpHeader");
            return Array.from(t3).reduce(function(e4, t4) {
              var r2, n2, i2 = null == (r2 = t4.querySelector("name")) ? void 0 : r2.textContent, o2 = null == (n2 = t4.querySelector("value")) ? void 0 : n2.textContent;
              return i2 && o2 && (e4[i2] = o2), e4;
            }, {});
          }
          function vr(e3) {
            var t3 = String.fromCharCode.apply(null, new Uint16Array(e3)), r2 = new DOMParser().parseFromString(t3, "application/xml");
            return { headers: pr(r2), body: hr(r2) };
          }
          function gr(e3, t3) {
            var r2 = "undefined" != typeof Symbol && e3[Symbol.iterator] || e3["@@iterator"];
            if (r2) return (r2 = r2.call(e3)).next.bind(r2);
            if (Array.isArray(e3) || (r2 = (function(e4, t4) {
              if (e4) {
                if ("string" == typeof e4) return mr(e4, t4);
                var r3 = {}.toString.call(e4).slice(8, -1);
                return "Object" === r3 && e4.constructor && (r3 = e4.constructor.name), "Map" === r3 || "Set" === r3 ? Array.from(e4) : "Arguments" === r3 || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(r3) ? mr(e4, t4) : void 0;
              }
            })(e3)) || t3 && e3 && "number" == typeof e3.length) {
              r2 && (e3 = r2);
              var n2 = 0;
              return function() {
                return n2 >= e3.length ? { done: true } : { done: false, value: e3[n2++] };
              };
            }
            throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
          }
          function mr(e3, t3) {
            (null == t3 || t3 > e3.length) && (t3 = e3.length);
            for (var r2 = 0, n2 = Array(t3); r2 < t3; r2++) n2[r2] = e3[r2];
            return n2;
          }
          var yr = [{ initDataTypes: ["cenc"], audioCapabilities: [{ contentType: 'audio/mp4;codecs="mp4a.40.2"' }], videoCapabilities: [{ robustness: "SW_SECURE_CRYPTO", contentType: 'video/mp4;codecs="avc1.42E01E"' }] }], br = [{ initDataTypes: ["sinf", "skd"], audioCapabilities: [{ contentType: 'audio/mp4;codecs="mp4a.40.2"' }], videoCapabilities: [{ contentType: 'video/mp4;codecs="avc1.42E01E"' }] }], Er = (function() {
            function e3(e4) {
              var t4 = this;
              this.video = void 0, this.listener = void 0, this.logger = void 0, this.cdmSupport = void 0, this.selectedCDM = void 0, this.mediaKeys = void 0, this.eventsToProcess = void 0, this.sessions = void 0, this.authXml = void 0, this.fairPlayCertificate = void 0, this.fairPlayContentId = void 0, this.mediaSinkMode = void 0, this.video = e4.video, this.listener = e4.listener, this.logger = (0, K.createLogger)({ name: "drm-manager" }), this.cdmSupport = null, this.selectedCDM = null, this.mediaKeys = void 0, this.eventsToProcess = [], this.sessions = [], this.authXml = null, this.fairPlayCertificate = null, this.fairPlayContentId = null, this.mediaSinkMode = null, this.video.addEventListener("encrypted", function(e5) {
                "passthrough" === t4.mediaSinkMode && t4.requestAuthXML(t4.video.src), t4.handleEncrypted(e5).catch(function(e6) {
                  t4.handleError(e6);
                });
              });
            }
            var t3 = e3.prototype;
            return t3.configure = function(e4) {
              var t4 = e4.mode, r2 = e4.path, n2 = e4.isProtected;
              "passthrough" === t4 && this.reset(), this.mediaSinkMode = t4, r2 && n2 && this.requestAuthXML(r2);
            }, t3.reset = function() {
              this.cdmSupport = null, this.selectedCDM = null, this.eventsToProcess = [], this.authXml = null, this.fairPlayCertificate = null, this.fairPlayContentId = null, this.mediaSinkMode = null, this.closeSessions();
            }, t3.isProtected = function() {
              return null !== this.authXml;
            }, t3.requestAuthXML = function(e4) {
              var t4 = this;
              if (!this.authXml) {
                var r2 = new URL(e4), n2 = r2.pathname.split("/"), i2 = n2[n2.length - 1].split(".")[0], o2 = (function(e5) {
                  var t5 = new URL(e5).searchParams, r3 = {};
                  return t5.forEach(function(e6, t6) {
                    r3[zt(t6)] = e6 ? zt(e6) : "";
                  }), r3;
                })(e4), a2 = o2.token, s2 = o2.sig, u2 = "https://" + r2.host + "/api/authxml/" + i2 + "?token=" + encodeURIComponent(a2) + "&sig=" + s2;
                this.authXml = Vt(u2, { method: "GET", responseType: "text" }).catch(function(e5) {
                  t4.handleError(Object.assign({ code: e5 }, xt));
                });
              }
            }, t3.handleError = function(e4) {
              this.listener.onSinkError({ value: e4.value || 4, code: e4.code || 0, message: e4.message || "" });
            }, t3.hasSession = function(e4) {
              if (!e4) return false;
              for (var t4, r2 = gr(this.sessions); !(t4 = r2()).done; ) {
                var n2 = t4.value;
                if (n2.initData && Nt(n2.initData, e4)) return true;
              }
              return false;
            }, t3.handleEncrypted = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2, a2, s2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.hasSession(t4.initData)) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return");
                    case 1:
                      if (this.logger.debug('Encrypted event type: "' + t4.initDataType + '"'), "sinf" !== t4.initDataType && "skd" !== t4.initDataType) {
                        e6.next = 9;
                        break;
                      }
                      if (!("requestMediaKeySystemAccess" in navigator)) {
                        e6.next = 6;
                        break;
                      }
                      if (void 0 !== this.mediaKeys) {
                        e6.next = 4;
                        break;
                      }
                      return this.mediaKeys = null, e6.next = 2, this.handleFairPlay(t4);
                    case 2:
                      if ((r2 = e6.sent).ok) {
                        e6.next = 3;
                        break;
                      }
                      return this.handleError(r2.error), e6.abrupt("return");
                    case 3:
                      e6.next = 5;
                      break;
                    case 4:
                      this.addSession(t4);
                    case 5:
                      e6.next = 8;
                      break;
                    case 6:
                      return e6.next = 7, this.handleLegacyFairPlay(t4);
                    case 7:
                      if ((n2 = e6.sent).ok) {
                        e6.next = 8;
                        break;
                      }
                      return this.handleError(n2.error), e6.abrupt("return");
                    case 8:
                      return e6.abrupt("return");
                    case 9:
                      if (null === this.cdmSupport && (this.cdmSupport = Ut(t4.initData)), void 0 !== this.mediaKeys) {
                        e6.next = 17;
                        break;
                      }
                      return this.mediaKeys = null, e6.next = 10, Sr(this.cdmSupport);
                    case 10:
                      if ((i2 = e6.sent).ok) {
                        e6.next = 11;
                        break;
                      }
                      return this.handleError(i2.error), e6.abrupt("return");
                    case 11:
                      return this.selectedCDM = St[i2.value.keySystem], e6.next = 12, kr(i2.value);
                    case 12:
                      if ((o2 = e6.sent).ok) {
                        e6.next = 13;
                        break;
                      }
                      return this.handleError(Object.assign({ code: o2.error }, At)), e6.abrupt("return");
                    case 13:
                      if (this.selectedCDM !== Et.FAIRPLAY) {
                        e6.next = 15;
                        break;
                      }
                      return e6.next = 14, this.setFairPlayCertificate(o2.value);
                    case 14:
                      if ((a2 = e6.sent).ok) {
                        e6.next = 15;
                        break;
                      }
                      return this.handleError(a2.error), e6.abrupt("return");
                    case 15:
                      return e6.next = 16, this.setMediaKeys(o2.value);
                    case 16:
                      if ((s2 = e6.sent).ok) {
                        e6.next = 17;
                        break;
                      }
                      return this.handleError(s2.error), e6.abrupt("return");
                    case 17:
                      this.addSession(t4);
                    case 18:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.setMediaKeys = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.video.mediaKeys || this.video.mediaKeys === t4) {
                        e6.next = 2;
                        break;
                      }
                      return e6.next = 1, Ir(this.video, null);
                    case 1:
                      if ((r2 = e6.sent).ok) {
                        e6.next = 2;
                        break;
                      }
                      return this.mediaKeys = void 0, e6.abrupt("return", (0, K.err)(Object.assign({ code: r2.error }, At)));
                    case 2:
                      return this.mediaKeys = t4, this.eventsToProcess.forEach(function(e7) {
                        return i2.createSessionRequest(e7).catch(function() {
                          i2.handleError(At);
                        });
                      }), this.eventsToProcess = [], e6.next = 3, Ir(this.video, this.mediaKeys);
                    case 3:
                      if ((n2 = e6.sent).ok) {
                        e6.next = 4;
                        break;
                      }
                      return this.mediaKeys = void 0, e6.abrupt("return", (0, K.err)(Object.assign({ code: n2.error }, At)));
                    case 4:
                      return e6.abrupt("return", (0, K.ok)(void 0));
                    case 5:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.addSession = function(e4) {
              var t4 = this;
              this.mediaKeys ? this.createSessionRequest(e4).catch(function() {
                t4.handleError(At);
              }) : this.eventsToProcess.push(e4);
            }, t3.createSessionRequest = function(e4) {
              var t4, r2 = this, n2 = e4.initDataType, i2 = e4.initData, o2 = null == (t4 = this.mediaKeys) ? void 0 : t4.createSession();
              return o2 ? (this.sessions.push({ mediaKeySession: o2, initDataType: n2, initData: i2 }), o2.addEventListener("message", function(e5) {
                r2.handleMessage(e5).catch(function(e6) {
                  r2.handleError(e6);
                });
              }), o2.addEventListener("keystatuseschange", function(e5) {
                return r2.handleKeyStatusesChange(e5, i2);
              }), o2.generateRequest(n2, i2)) : Promise.reject();
            }, t3.handleKeyStatusesChange = function(e4, t4) {
              var r2 = this, n2 = e4.target, i2 = false;
              n2.keyStatuses.forEach(function(e5) {
                switch (e5) {
                  case "expired":
                    i2 = true;
                    break;
                  case "internal-error":
                    r2.handleError(It);
                }
              }), i2 && this.closeSession(n2, t4);
            }, t3.removeSession = function(e4) {
              for (var t4 = 0; t4 < this.sessions.length; t4++) {
                var r2 = this.sessions[t4];
                if (e4 && r2.initData === e4) return void this.sessions.splice(t4, 1);
              }
            }, t3.closeSessions = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4, r2, n2, i2, o2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      t4 = gr(this.sessions);
                    case 1:
                      if ((r2 = t4()).done) {
                        e6.next = 3;
                        break;
                      }
                      return n2 = r2.value, e6.next = 2, this.closeSession(n2.mediaKeySession, n2.initData);
                    case 2:
                      e6.next = 1;
                      break;
                    case 3:
                      if (this.sessions = [], !this.mediaKeys) {
                        e6.next = 5;
                        break;
                      }
                      return e6.next = 4, Ir(this.video, null);
                    case 4:
                      if (i2 = e6.sent, this.mediaKeys = void 0, i2.ok) {
                        e6.next = 5;
                        break;
                      }
                      return o2 = (0, K.err)(Object.assign({ code: i2.error }, At)), this.handleError(o2.error), e6.abrupt("return");
                    case 5:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.closeSession = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.next = 1, t4.close().then(function() {
                        n2.removeSession(r2);
                      }).catch(function() {
                        n2.removeSession(r2);
                      });
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.handleMessage = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return r2 = t4.target, e6.next = 1, this.generateLicense(t4.message);
                    case 1:
                      (n2 = e6.sent).ok ? (this.logger.info("Update MediaKeySession"), r2.update(n2.value).catch(function() {
                        i2.handleError(_t);
                      })) : this.handleError(n2.error);
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.generateLicense = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.selectedCDM !== Et.CLEARKEY) {
                        e6.next = 1;
                        break;
                      }
                      return r2 = JSON.parse(new TextDecoder().decode(t4)), n2 = r2.kids.map(function(e7) {
                        return { kty: "oct", alg: "A128KW", kid: e7, k: e7 };
                      }), i2 = new TextEncoder().encode(JSON.stringify({ keys: n2 })), e6.abrupt("return", (0, K.ok)(i2.buffer));
                    case 1:
                      if (this.selectedCDM !== Et.FAIRPLAY) {
                        e6.next = 3;
                        break;
                      }
                      return e6.next = 2, this.generateFairPlayLicense(t4);
                    case 2:
                    case 5:
                      return e6.abrupt("return", e6.sent);
                    case 3:
                      if (!this.authXml) {
                        e6.next = 6;
                        break;
                      }
                      return e6.next = 4, this.authXml;
                    case 4:
                      return o2 = e6.sent, e6.next = 5, this.requestLicense(t4, o2);
                    case 6:
                      return e6.abrupt("return", (0, K.err)(xt));
                    case 7:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.requestLicense = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return i2 = { method: "POST", responseType: "arraybuffer", body: t4, headers: { customdata: r2, "Content-Type": "application/octet-stream" } }, this.selectedCDM === Et.PLAYREADY && (o2 = vr(t4), i2.body = o2.body, i2.headers = Object.assign(i2.headers, o2.headers)), e6.next = 1, xr((null == (n2 = this.selectedCDM) ? void 0 : n2.licenseUrl) || "", i2);
                    case 1:
                      if ((a2 = e6.sent).ok) {
                        e6.next = 7;
                        break;
                      }
                      u2 = a2.error, e6.next = 0 === u2 ? 2 : 404 === u2 ? 3 : 403 === u2 ? 4 : 5;
                      break;
                    case 2:
                      return s2 = Ct, e6.abrupt("continue", 6);
                    case 3:
                      return s2 = wt, e6.abrupt("continue", 6);
                    case 4:
                      return s2 = kt, e6.abrupt("continue", 6);
                    case 5:
                      return s2 = Pt, e6.abrupt("continue", 6);
                    case 6:
                      return e6.abrupt("return", (0, K.err)(Object.assign({ code: a2.error }, s2)));
                    case 7:
                      return e6.abrupt("return", (0, K.ok)(a2.value));
                    case 8:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.handleFairPlay = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2, a2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.logger.info("FairPlay initialization started"), !t4.initData) {
                        e6.next = 2;
                        break;
                      }
                      if ((r2 = qt(t4.initData, t4.initDataType)).ok) {
                        e6.next = 1;
                        break;
                      }
                      return this.logger.error("Failed to extract content ID"), e6.abrupt("return", (0, K.err)(Rt));
                    case 1:
                      this.fairPlayContentId = r2.value, this.logger.debug("Content ID: " + this.fairPlayContentId), e6.next = 3;
                      break;
                    case 2:
                      return this.logger.error("No init data provided"), e6.abrupt("return", (0, K.err)(Lt));
                    case 3:
                      return this.selectedCDM = Et.FAIRPLAY, e6.next = 4, _r(Et.FAIRPLAY.keySystem, br);
                    case 4:
                      if ((n2 = e6.sent).ok) {
                        e6.next = 5;
                        break;
                      }
                      return this.logger.error("Failed to get media key system access"), e6.abrupt("return", (0, K.err)(Object.assign({ code: n2.error }, At)));
                    case 5:
                      return e6.next = 6, kr(n2.value);
                    case 6:
                      if ((i2 = e6.sent).ok) {
                        e6.next = 7;
                        break;
                      }
                      return this.logger.error("Failed to create media keys"), e6.abrupt("return", (0, K.err)(Object.assign({ code: i2.error }, At)));
                    case 7:
                      return e6.next = 8, this.setFairPlayCertificate(i2.value);
                    case 8:
                      if ((o2 = e6.sent).ok) {
                        e6.next = 9;
                        break;
                      }
                      return this.logger.error("Failed to set certificate"), e6.abrupt("return", (0, K.err)(o2.error));
                    case 9:
                      return e6.next = 10, this.setMediaKeys(i2.value);
                    case 10:
                      if ((a2 = e6.sent).ok) {
                        e6.next = 11;
                        break;
                      }
                      return this.logger.error("Failed to set media keys"), e6.abrupt("return", (0, K.err)(a2.error));
                    case 11:
                      return this.logger.info("FairPlay initialization complete"), this.addSession(t4), e6.abrupt("return", (0, K.ok)(void 0));
                    case 12:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.setFairPlayCertificate = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.fairPlayCertificate) {
                        e6.next = 3;
                        break;
                      }
                      return this.logger.info("Fetching FairPlay certificate"), e6.next = 1, this.fetchFairPlayCertificate();
                    case 1:
                      if ((r2 = e6.sent).ok) {
                        e6.next = 2;
                        break;
                      }
                      return this.logger.error("Failed to fetch certificate"), e6.abrupt("return", (0, K.err)(r2.error));
                    case 2:
                      this.fairPlayCertificate = r2.value;
                    case 3:
                      return e6.next = 4, Pr(t4, this.fairPlayCertificate);
                    case 4:
                      if ((n2 = e6.sent).ok) {
                        e6.next = 5;
                        break;
                      }
                      return this.logger.error("Failed to set server certificate"), e6.abrupt("return", (0, K.err)(Object.assign({ code: n2.error }, Mt)));
                    case 5:
                      return this.logger.info("Certificate configured"), e6.abrupt("return", (0, K.ok)(void 0));
                    case 6:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.fetchFairPlayCertificate = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return this.logger.debug("Certificate URL: " + Et.FAIRPLAY.certUrl), e6.next = 1, xr(Et.FAIRPLAY.certUrl || "", { method: "GET", responseType: "arraybuffer", headers: { Pragma: "Cache-Control: no-cache", "Cache-Control": "max-age=0" } });
                    case 1:
                      if ((t4 = e6.sent).ok) {
                        e6.next = 2;
                        break;
                      }
                      return this.logger.error("Certificate request failed: " + t4.error), e6.abrupt("return", (0, K.err)(Object.assign({ code: t4.error }, Mt)));
                    case 2:
                      return e6.abrupt("return", (0, K.ok)(t4.value));
                    case 3:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.generateFairPlayLicense = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2, a2, s2, u2, c2, l2, d2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.logger.info("Generating FairPlay license"), this.authXml) {
                        e6.next = 1;
                        break;
                      }
                      return this.logger.error("No auth XML available"), e6.abrupt("return", (0, K.err)(xt));
                    case 1:
                      if (this.fairPlayContentId) {
                        e6.next = 2;
                        break;
                      }
                      return this.logger.error("No content ID available"), e6.abrupt("return", (0, K.err)(Dt));
                    case 2:
                      return e6.next = 3, this.authXml;
                    case 3:
                      if (r2 = e6.sent, n2 = this.fairPlayContentId, i2 = Et.FAIRPLAY.licenseUrl, (o2 = Kt(new Uint8Array(t4))).ok) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(Rt));
                    case 4:
                      return a2 = "spc=" + o2.value + "&assetId=" + n2, s2 = { method: "POST", body: a2, responseType: "text", headers: { "Content-Type": "application/x-www-form-urlencoded", customdata: r2 } }, this.logger.debug("License URL: " + i2), e6.next = 5, xr(i2 || "", s2);
                    case 5:
                      if ((u2 = e6.sent).ok) {
                        e6.next = 6;
                        break;
                      }
                      return this.logger.error("License request failed: " + u2.error), e6.abrupt("return", (0, K.err)(Object.assign({ code: u2.error }, Ct)));
                    case 6:
                      if ("<ckc>" === (c2 = u2.value.trim()).substr(0, 5) && "</ckc>" === c2.substr(-6) && (c2 = c2.slice(5, -6)), this.logger.info("License generated"), (l2 = Wt(c2)).ok) {
                        e6.next = 7;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(Rt));
                    case 7:
                      return d2 = new ArrayBuffer(l2.value.length), new Uint8Array(d2).set(l2.value), e6.abrupt("return", (0, K.ok)(d2));
                    case 8:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.handleLegacyFairPlay = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return this.selectedCDM = Et.FAIRPLAY, e6.next = 1, this.fetchFairPlayCertificate();
                    case 1:
                      if ((r2 = e6.sent).ok) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(r2.error));
                    case 2:
                      return e6.next = 3, this.setupWebKitMediaKeys(t4, r2.value);
                    case 3:
                      return e6.abrupt("return", e6.sent);
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.setupWebKitMediaKeys = function(e4, t4) {
              var r2 = this;
              if (!e4.initData) return Promise.resolve((0, K.err)(Dt));
              this.video.webkitKeys || this.video.webkitSetMediaKeys(new window.WebKitMediaKeys(Et.FAIRPLAY.keySystem));
              var n2 = (function(e5) {
                try {
                  var t5 = Wt(JSON.parse(String.fromCharCode.apply(null, e5)).sinf[0]);
                  if (!t5.ok) return (0, K.err)("Failed to decode sinf: " + t5.error);
                  var r3 = Bt(t5.value, "schi");
                  if (!r3.ok) return (0, K.err)("Failed to find schi box: " + r3.error);
                  var n3 = Bt(r3.value, "tenc");
                  return n3.ok ? (0, K.ok)(jt(n3.value.subarray(8, 24))) : (0, K.err)("Failed to find tenc box: " + n3.error);
                } catch (e6) {
                  return (0, K.err)("Failed to parse init data: " + (e6 instanceof Error ? e6.message : String(e6)));
                }
              })(e4.initData);
              if (!n2.ok) return Promise.resolve((0, K.err)(Rt));
              var i2 = this.video.webkitKeys.createSession("video/mp4", e4.initData);
              return i2.contentId = n2.value, new Promise(function(e5) {
                r2.video.webkitKeys && i2 ? (i2.addEventListener("webkitkeymessage", function(i3) {
                  var o2 = i3.target;
                  "certificate" === String.fromCharCode.apply(null, i3.message) ? o2.update(new Uint8Array(t4)) : r2.getWebkitLicense(i3.message, n2.value).then(function(t5) {
                    if (t5.ok) {
                      var r3 = t5.value.trim();
                      "<ckc>" === r3.substr(0, 5) && "</ckc>" === r3.substr(-6) && (r3 = r3.slice(5, -6)), o2.update(Wt(r3));
                    } else e5((0, K.err)(t5.error));
                  }).catch(function() {
                    return e5((0, K.err)(Ct));
                  });
                }), i2.addEventListener("webkitkeyadded", function() {
                  return e5((0, K.ok)(void 0));
                }), i2.addEventListener("webkitkeyerror", function() {
                  return e5((0, K.err)(At));
                })) : e5((0, K.err)(At));
              });
            }, t3.getWebkitLicense = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.authXml) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(xt));
                    case 1:
                      return e6.next = 2, this.authXml;
                    case 2:
                      if (n2 = e6.sent, i2 = Et.FAIRPLAY.licenseUrl, (o2 = Kt(t4)).ok) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(Rt));
                    case 3:
                      return a2 = "spc=" + o2.value + "&assetId=" + r2, s2 = { method: "POST", body: a2, responseType: "text", headers: { "Content-Type": "application/x-www-form-urlencoded", customdata: n2 } }, e6.next = 4, xr(i2 || "", s2);
                    case 4:
                      if ((u2 = e6.sent).ok) {
                        e6.next = 5;
                        break;
                      }
                      return e6.abrupt("return", (0, K.err)(Object.assign({ code: u2.error }, Ct)));
                    case 5:
                      return e6.abrupt("return", (0, K.ok)(u2.value));
                    case 6:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), e3;
          })();
          function Sr(e3) {
            return Tr.apply(this, arguments);
          }
          function Tr() {
            return (Tr = Ne()(Ue().mark(function e3(t3) {
              var r2, n2, i2, o2, a2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    if (null !== t3 && 0 !== t3.length) {
                      e4.next = 1;
                      break;
                    }
                    return e4.abrupt("return", (0, K.err)(Dt));
                  case 1:
                    r2 = gr(t3);
                  case 2:
                    if ((n2 = r2()).done) {
                      e4.next = 7;
                      break;
                    }
                    return i2 = n2.value, e4.prev = 3, o2 = i2 === Et.FAIRPLAY ? br : yr, e4.next = 4, navigator.requestMediaKeySystemAccess(i2.keySystem, o2);
                  case 4:
                    return a2 = e4.sent, e4.abrupt("return", (0, K.ok)(a2));
                  case 5:
                    e4.prev = 5, e4.catch(3);
                  case 6:
                    e4.next = 2;
                    break;
                  case 7:
                    return e4.abrupt("return", (0, K.err)(Tt));
                  case 8:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[3, 5]]);
            }))).apply(this, arguments);
          }
          function _r(e3, t3) {
            return Cr.apply(this, arguments);
          }
          function Cr() {
            return (Cr = Ne()(Ue().mark(function e3(t3, r2) {
              var n2, i2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    return e4.prev = 0, e4.next = 1, navigator.requestMediaKeySystemAccess(t3, r2);
                  case 1:
                    return n2 = e4.sent, e4.abrupt("return", (0, K.ok)(n2));
                  case 2:
                    return e4.prev = 2, i2 = e4.catch(0), e4.abrupt("return", (0, K.err)(i2));
                  case 3:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[0, 2]]);
            }))).apply(this, arguments);
          }
          function kr(e3) {
            return wr.apply(this, arguments);
          }
          function wr() {
            return (wr = Ne()(Ue().mark(function e3(t3) {
              var r2, n2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    return e4.prev = 0, e4.next = 1, t3.createMediaKeys();
                  case 1:
                    return r2 = e4.sent, e4.abrupt("return", (0, K.ok)(r2));
                  case 2:
                    return e4.prev = 2, n2 = e4.catch(0), e4.abrupt("return", (0, K.err)(n2));
                  case 3:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[0, 2]]);
            }))).apply(this, arguments);
          }
          function Pr(e3, t3) {
            return Ar.apply(this, arguments);
          }
          function Ar() {
            return (Ar = Ne()(Ue().mark(function e3(t3, r2) {
              var n2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    if (e4.prev = 0, !t3.setServerCertificate) {
                      e4.next = 1;
                      break;
                    }
                    return e4.next = 1, t3.setServerCertificate(r2);
                  case 1:
                    return e4.abrupt("return", (0, K.ok)(void 0));
                  case 2:
                    return e4.prev = 2, n2 = e4.catch(0), e4.abrupt("return", (0, K.err)(n2));
                  case 3:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[0, 2]]);
            }))).apply(this, arguments);
          }
          function Ir(e3, t3) {
            return Dr.apply(this, arguments);
          }
          function Dr() {
            return (Dr = Ne()(Ue().mark(function e3(t3, r2) {
              var n2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    return e4.prev = 0, e4.next = 1, t3.setMediaKeys(r2);
                  case 1:
                    return e4.abrupt("return", (0, K.ok)(void 0));
                  case 2:
                    return e4.prev = 2, n2 = e4.catch(0), e4.abrupt("return", (0, K.err)(n2));
                  case 3:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[0, 2]]);
            }))).apply(this, arguments);
          }
          function xr(e3, t3) {
            return Mr.apply(this, arguments);
          }
          function Mr() {
            return (Mr = Ne()(Ue().mark(function e3(t3, r2) {
              var n2, i2;
              return Ue().wrap(function(e4) {
                for (; ; ) switch (e4.prev = e4.next) {
                  case 0:
                    return e4.prev = 0, e4.next = 1, Vt(t3, r2);
                  case 1:
                    return n2 = e4.sent, e4.abrupt("return", (0, K.ok)(n2));
                  case 2:
                    return e4.prev = 2, i2 = e4.catch(0), e4.abrupt("return", (0, K.err)(i2));
                  case 3:
                  case "end":
                    return e4.stop();
                }
              }, e3, null, [[0, 2]]);
            }))).apply(this, arguments);
          }
          var Rr = require_inheritsLoose(), Lr = r.n(Rr), Or = (function() {
            function e3() {
            }
            var t3 = e3.prototype;
            return t3.addTrack = function(e4) {
            }, t3.bufferDuration = function() {
              return 0;
            }, t3.buffered = function() {
              return { start: 0, end: 0 };
            }, t3.getBufferedRanges = function(e4) {
              return [];
            }, t3.captureGesture = function() {
            }, t3.configure = function(e4) {
            }, t3.delete = function() {
            }, t3.endOfStream = function() {
            }, t3.enqueue = function(e4) {
            }, t3.getCurrentTime = function() {
              return 0;
            }, t3.getPlaybackRate = function() {
              return 0;
            }, t3.getVolume = function() {
              return 0;
            }, t3.invoke = function(e4) {
              this[e4.name].call(this, e4.arg);
            }, t3.isMuted = function() {
              return false;
            }, t3.onSourceDurationChanged = function(e4) {
            }, t3.onPlayerConfigurationChanged = function(e4) {
            }, t3.pause = function() {
            }, t3.play = function() {
            }, t3.reinit = function() {
            }, t3.remove = function(e4) {
            }, t3.seekTo = function(e4) {
            }, t3.setMuted = function(e4) {
            }, t3.setPlaybackRate = function(e4) {
            }, t3.setTimestampOffset = function(e4) {
            }, t3.setVolume = function(e4) {
            }, t3.changeSrc = function(e4) {
            }, t3.changeSrcObj = function(e4) {
            }, t3.onSegmentDiscontinuity = function() {
            }, t3.getGapSkipStatistics = function() {
              return { count: 0, durationInSeconds: 0 };
            }, e3;
          })(), Nr = z("chromecast-sink").configureLogger, Fr = "pc-chromecast-sender", Ur = (function(e3) {
            function t3(r2) {
              var n3;
              return (n3 = e3.call(this) || this).remotePlayer = void 0, n3.remotePlayerController = void 0, n3.listener = void 0, n3.seekTime = void 0, n3.currentDuration = void 0, n3.listener = r2, n3.currentDuration = 0, t3.prepareCastContext().then(function() {
                n3.remotePlayer = new cast.framework.RemotePlayer(), n3.remotePlayerController = new cast.framework.RemotePlayerController(n3.remotePlayer);
              }).catch(function() {
                n3.listener.onSessionError();
              }), n3;
            }
            Lr()(t3, e3), t3.canCast = function() {
              return xe().chrome;
            }, t3.stopLookingForRemotePlaybackDevices = function(e4) {
              void 0 !== window.cast && window.cast && window.cast.framework && cast.framework.CastContext.getInstance().removeEventListener(cast.framework.CastContextEventType.CAST_STATE_CHANGED, e4);
            }, t3.lookForRemotePlaybackDevices = (function() {
              var e4 = Ne()(Ue().mark(function e5(r2) {
                var n3, i2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.prev = 0, e6.next = 1, t3.prepareCastContext();
                    case 1:
                      return n3 = e6.sent, i2 = function(e7) {
                        switch (e7.castState) {
                          case cast.framework.CastState.NO_DEVICES_AVAILABLE:
                            break;
                          case cast.framework.CastState.NOT_CONNECTED:
                            r2.onRemoteDevice(true);
                            break;
                          case cast.framework.CastState.CONNECTED:
                            var t4 = n3.getCurrentSession();
                            t4 && t4.getSessionState() === cast.framework.SessionState.SESSION_RESUMED && r2.onRemoteReconnect();
                        }
                      }, n3.addEventListener(cast.framework.CastContextEventType.CAST_STATE_CHANGED, i2), n3.setOptions({ receiverApplicationId: "B3DCF968", autoJoinPolicy: chrome.cast.AutoJoinPolicy.TAB_AND_ORIGIN_SCOPED }), e6.abrupt("return", i2);
                    case 2:
                      e6.prev = 2, e6.catch(0), r2.onRemoteDevice(false);
                    case 3:
                    case "end":
                      return e6.stop();
                  }
                }, e5, null, [[0, 2]]);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.prepareCastContext = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (void 0 === window.cast || !window.cast || !window.cast.framework) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", Promise.resolve(cast.framework.CastContext.getInstance()));
                    case 1:
                      return e6.abrupt("return", new Promise(function(e7, t4) {
                        if (r.g.__onGCastApiAvailable = function(r2) {
                          r2 ? e7(cast.framework.CastContext.getInstance()) : t4();
                        }, !document.getElementById(Fr)) {
                          var n3 = document.createElement("script");
                          n3.id = Fr, n3.onerror = function() {
                            document.body.removeChild(n3), r.g.__onGCastApiAvailable = function() {
                            }, t4();
                          }, n3.async = true, n3.src = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1", document.body.appendChild(n3);
                        }
                      }));
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })();
            var n2 = t3.prototype;
            return n2.configure = (function() {
              var e4 = Ne()(Ue().mark(function e5(r2) {
                var n3, i2, o2, a2, s2, u2, c2, l2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return n3 = r2.path, Nr.debug("configure called", { path: n3 }), e6.prev = 1, e6.next = 2, t3.prepareCastContext();
                    case 2:
                      if (o2 = e6.sent, a2 = o2.getCurrentSession()) {
                        e6.next = 4;
                        break;
                      }
                      return e6.next = 3, o2.requestSession();
                    case 3:
                      a2 = o2.getCurrentSession(), this.setupRemotePlayerListeners(a2), e6.next = 5;
                      break;
                    case 4:
                      a2.getSessionState() === cast.framework.SessionState.SESSION_RESUMED && this.setupRemotePlayerListeners(a2);
                    case 5:
                      return (s2 = new chrome.cast.media.MediaInfo(n3, "")).streamType = chrome.cast.media.StreamType.BUFFERED, u2 = new chrome.cast.media.GenericMediaMetadata(), s2.metadata = u2, s2.customData = { analytics: { chromecast_sender: "player-core", platform: "web" } }, this.remotePlayerController.stop(), c2 = new chrome.cast.media.LoadRequest(s2), this.seekTime > 0 && (c2.currentTime = this.seekTime, this.seekTime = 0), this.currentDuration = 0, e6.next = 6, null == (i2 = a2) ? void 0 : i2.loadMedia(c2);
                    case 6:
                      e6.next = 8;
                      break;
                    case 7:
                      return e6.prev = 7, l2 = e6.catch(1), e6.abrupt("return", this.handleError(l2));
                    case 8:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[1, 7]]);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), n2.stopMedia = (function() {
              var e4 = Ne()(Ue().mark(function e5(r2) {
                var n3, i2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return void 0 === r2 && (r2 = true), e6.next = 1, t3.prepareCastContext();
                    case 1:
                      n3 = e6.sent, (i2 = n3.getCurrentSession()) && i2.getSessionState() !== cast.framework.SessionState.SESSION_RESUMED && i2.endSession(r2);
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), n2.invoke = function(e4) {
              this[e4.name].call(this, e4.arg);
            }, n2.play = function() {
              this.remotePlayer && this.remotePlayer.isPaused && this.remotePlayerController.playOrPause();
            }, n2.pause = function() {
              this.remotePlayer && !this.remotePlayer.isPaused && this.remotePlayerController.playOrPause();
            }, n2.seekTo = function(e4) {
              this.remotePlayer && (this.remotePlayer.playerState !== chrome.cast.media.PlayerState.IDLE ? (this.remotePlayer.currentTime = e4, this.remotePlayerController.seek()) : this.seekTime = e4);
            }, n2.getCurrentTime = function() {
              return this.remotePlayer ? this.remotePlayer.currentTime : 0;
            }, n2.delete = function() {
              this.remotePlayer && this.stopMedia();
            }, n2.setMuted = function(e4) {
              this.remotePlayer && e4 !== this.remotePlayer.isMuted && this.remotePlayerController.muteOrUnmute();
            }, n2.isMuted = function() {
              return !!this.remotePlayer && this.remotePlayer.isMuted;
            }, n2.setVolume = function(e4) {
              this.remotePlayer && (this.remotePlayer.volumeLevel = e4, this.remotePlayerController.setVolumeLevel());
            }, n2.getVolume = function() {
              return this.remotePlayer ? this.remotePlayer.volumeLevel : 0;
            }, n2.getDevice = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var r2, n3;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.next = 1, t3.prepareCastContext();
                    case 1:
                      return r2 = e6.sent, n3 = r2.getCurrentSession(), e6.abrupt("return", (null == n3 ? void 0 : n3.getCastDevice().friendlyName) || "");
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), n2.setupRemotePlayerListeners = function(e4) {
              var t4 = this, r2 = (function() {
                var r3 = Ne()(Ue().mark(function r4() {
                  var n4, i3;
                  return Ue().wrap(function(r5) {
                    for (; ; ) switch (r5.prev = r5.next) {
                      case 0:
                        (n4 = e4.getMediaSession()) && ((i3 = n4.media) && 0 === t4.currentDuration && null === i3.duration && (t4.currentDuration = 1 / 0, t4.listener.onSinkDurationChanged(t4.currentDuration)), t4.listener.onSinkTimeUpdate());
                      case 1:
                      case "end":
                        return r5.stop();
                    }
                  }, r4);
                }));
                return function() {
                  return r3.apply(this, arguments);
                };
              })(), n3 = function() {
                switch (t4.remotePlayer.playerState) {
                  case chrome.cast.media.PlayerState.BUFFERING:
                    t4.listener.onSinkBuffering();
                    break;
                  case chrome.cast.media.PlayerState.PLAYING:
                    t4.listener.onSinkPlaying(false);
                    break;
                  case chrome.cast.media.PlayerState.IDLE:
                    var r3 = e4.getMediaSession();
                    r3 && r3.idleReason === chrome.cast.media.IdleReason.FINISHED && t4.listener.onSinkEnded();
                }
              }, i2 = function() {
                t4.listener.onSinkVolumeChanged(t4.remotePlayer.volumeLevel, true);
              }, o2 = function() {
                t4.listener.onSinkMutedChanged(t4.remotePlayer.isMuted);
              }, a2 = function() {
                0 !== t4.remotePlayer.duration && (t4.currentDuration = t4.remotePlayer.duration, t4.listener.onSinkDurationChanged(t4.currentDuration));
              }, s2 = function() {
                t4.remotePlayerController.removeEventListener(cast.framework.RemotePlayerEventType.CURRENT_TIME_CHANGED, r2), t4.remotePlayerController.removeEventListener(cast.framework.RemotePlayerEventType.PLAYER_STATE_CHANGED, n3), t4.remotePlayerController.removeEventListener(cast.framework.RemotePlayerEventType.VOLUME_LEVEL_CHANGED, i2), t4.remotePlayerController.removeEventListener(cast.framework.RemotePlayerEventType.IS_MUTED_CHANGED, o2), t4.remotePlayerController.removeEventListener(cast.framework.RemotePlayerEventType.DURATION_CHANGED, a2), t4.listener.onSessionStop();
              };
              e4.addEventListener(cast.framework.SessionEventType.MEDIA_SESSION, function() {
                t4.remotePlayerController.addEventListener(cast.framework.RemotePlayerEventType.CURRENT_TIME_CHANGED, r2), t4.remotePlayerController.addEventListener(cast.framework.RemotePlayerEventType.PLAYER_STATE_CHANGED, n3), t4.remotePlayerController.addEventListener(cast.framework.RemotePlayerEventType.VOLUME_LEVEL_CHANGED, i2), t4.remotePlayerController.addEventListener(cast.framework.RemotePlayerEventType.IS_MUTED_CHANGED, o2), t4.remotePlayerController.addEventListener(cast.framework.RemotePlayerEventType.DURATION_CHANGED, a2), t4.listener.onSessionStarted(e4.getCastDevice().friendlyName);
              });
              var u2 = e4.getSessionObj();
              u2.addUpdateListener(function() {
                u2.status === chrome.cast.SessionStatus.STOPPED && s2();
              }), u2.addMediaListener(s2);
            }, n2.handleError = function(e4) {
              if (chrome.cast) switch (e4) {
                case chrome.cast.ErrorCode.SESSION_ERROR:
                  this.listener.onSessionError();
                  break;
                case chrome.cast.ErrorCode.RECEIVER_UNAVAILABLE:
                  this.listener.onRemoteDevice(false);
                  break;
                case chrome.cast.ErrorCode.LOAD_MEDIA_FAILED:
                  this.listener.onLoadMediaError();
                  break;
                case chrome.cast.ErrorCode.CANCEL:
                  this.listener.onUserCancel();
                  break;
                default:
                  this.listener.onSinkError({ value: 1, code: 0, message: "Error requesting chromecast session" });
              }
              else this.listener.onSinkError({ value: 1, code: 0, message: "Error loading chromecast SDK" });
            }, t3;
          })(Or), Vr = (function(e3) {
            function t3(t4, r3) {
              var n2;
              return (n2 = e3.call(this) || this).listener = t4, n2.video = r3, n2.unsubscribe = void 0, n2.unsubscribe = U(r3, "error", n2.onVideoError.bind(n2)), n2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.onVideoError = function() {
              var e4, t4, r3 = null != (e4 = null == this || null == (t4 = this.video) ? void 0 : t4.error) ? e4 : {}, n2 = r3.code, i2 = void 0 === n2 ? -1 : n2, o2 = r3.message, a2 = void 0 === o2 ? "" : o2;
              this.listener.onSinkError({ value: i2, code: i2, message: a2 });
            }, r2.seekTo = function(e4) {
              this.video.currentTime = e4;
            }, r2.setPlaybackRate = function(e4) {
              this.video.playbackRate = e4;
            }, r2.setVolume = function(e4) {
              this.video.volume = e4;
            }, r2.getVolume = function() {
              return this.video.volume;
            }, r2.isMuted = function() {
              return this.video.muted;
            }, r2.setMuted = function(e4) {
              this.video.muted = e4;
            }, r2.getPlaybackRate = function() {
              return this.video.playbackRate;
            }, r2.delete = function() {
              var e4;
              null == (e4 = this.unsubscribe) || e4.call(this), this.unsubscribe = void 0;
            }, t3;
          })(Or), Br = (function() {
            function e3(e4, t4) {
              this.muted = void 0, this.video = void 0, this.listener = void 0, this.unsubscribes = [], this.expectingMutedChanged = false, this.expectingVolumeChanged = false, this.expectedRateChange = void 0, this.video = e4, this.listener = t4, this.muted = e4.muted, this.unsubscribes.push(U(e4, "volumechange", this.volumeChange.bind(this))), this.unsubscribes.push(U(e4, "ratechange", this.rateChange.bind(this)));
            }
            var t3 = e3.prototype;
            return t3.volumeChange = function() {
              var e4 = !this.expectingVolumeChanged;
              this.expectingMutedChanged = false, this.expectingVolumeChanged = false;
              var t4 = this.video.muted;
              this.muted !== t4 ? (this.muted = t4, this.listener.onSinkMutedChanged(t4)) : this.listener.onSinkVolumeChanged(this.video.volume, e4);
            }, t3.rateChange = function() {
              this.video.playbackRate !== this.expectedRateChange && this.listener.onSinkPlaybackRateChanged(this.video.playbackRate);
            }, t3.unsubscribe = function() {
              this.unsubscribes.forEach(function(e4) {
                return e4();
              });
            }, t3.onConfigure = function() {
              this.expectingVolumeChanged && (this.listener.onSinkVolumeChanged(this.video.volume, false), this.expectingVolumeChanged = false), this.expectingMutedChanged && (this.muted = this.video.muted, this.listener.onSinkMutedChanged(this.video.muted), this.expectingMutedChanged = false), this.expectedRateChange = void 0;
            }, t3.trackRPC = function(e4) {
              var t4 = e4.name, r2 = e4.arg;
              "setVolume" === t4 && this.video.volume !== r2 ? this.expectingVolumeChanged = true : "setMuted" === t4 && this.video.muted !== r2 ? this.expectingMutedChanged = true : "setPlaybackRate" === t4 && this.video.playbackRate !== r2 && (this.expectedRateChange = r2);
            }, e3;
          })(), Gr = require_lib(), jr = r.n(Gr), Hr = ("undefined" != typeof self ? self : "undefined" != typeof window ? window : void 0 !== r.g ? r.g : void 0).Promise || jr(), Wr = (function(e3) {
            function t3(t4, r3) {
              var n2;
              return (n2 = e3.call(this) || this).paused = true, n2.listener = void 0, n2.video = void 0, n2.unsubscribers = void 0, n2.lastVolumeChangeEvent = void 0, n2.gapSkipStats = { count: 0, durationInSeconds: 0 }, n2.video = t4, n2.listener = r3, n2.paused = true, n2.unsubscribers = [], n2.addListener("volumechange", n2.recordMuteChange.bind(n2)), n2.recordMuteChange(), n2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.pause = function() {
              this.paused = true, this.video.pause();
            }, r2.setPlaybackRate = function(e4) {
              this.video.playbackRate = e4;
            }, r2.getGapSkipStatistics = function() {
              return this.gapSkipStats;
            }, r2.delete = function() {
              this.unsubscribers.forEach(function(e4) {
                return e4();
              });
            }, r2.addListener = function(e4, t4, r3) {
              void 0 === r3 && (r3 = this.video), this.unsubscribers.push(U(r3, e4, t4));
            }, r2.recordMuteChange = function() {
              this.lastVolumeChangeEvent = { time: this.video.currentTime, muted: this.video.muted };
            }, r2.checkStopped = function(e4) {
              return !this.video.paused || this.video.ended || this.video.error || this.paused || this.listener.onSinkStop(e4 || this.unmuteAutopause()), false;
            }, r2.onGapSkipped = function(e4) {
              this.gapSkipStats.count++, this.gapSkipStats.durationInSeconds += e4;
            }, r2.unmuteAutopause = function() {
              var e4 = this.lastVolumeChangeEvent;
              return !this.video.muted && !e4.muted && this.video.currentTime === e4.time;
            }, t3;
          })(Or), Kr = (function(e3) {
            function t3(t4, r3, n2) {
              var i2;
              return void 0 === n2 && (n2 = {}), (i2 = e3.call(this, t4, r3) || this).onSinkBuffering = void 0, i2.logger = void 0, i2.intervalId = void 0, i2.idle = void 0, i2.lastPlayhead = void 0, i2.lastBufferEnd = void 0, i2.idleTimeout = void 0, i2.playAttempt = false, i2.seeking = false, i2.audioBufferList = void 0, i2.awaitingAutoplayCompletion = false, i2.config = void 0, i2.config = n2, i2.logger = (0, K.createLogger)({ name: "playback-monitor", enabled: true, levels: { debug: false, log: true, info: true, warn: true, error: true } }), i2.intervalId = 0, i2.idle = true, i2.lastPlayhead = 0, i2.lastBufferEnd = 0, i2.idleTimeout = -1, i2.audioBufferList = [], i2.bindEvents(), i2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.bindEvents = function() {
              var e4 = this;
              this.addListener("play", function() {
                return e4.onVideoPlay();
              }), this.addListener("pause", function() {
                return e4.onVideoPause();
              }), this.addListener("timeupdate", function() {
                return e4.onVideoTimeUpdate();
              }), this.addListener("ended", function() {
                return e4.onVideoEnded();
              }), this.addListener("error", function() {
                return e4.onVideoError();
              }), this.addListener("playing", function() {
                return e4.onVideoPlaying();
              }), this.addListener("seeking", function() {
                return e4.onVideoSeeking();
              });
            }, r2.delete = function() {
              e3.prototype.delete.call(this), this.audioBufferList = [], clearInterval(this.intervalId);
            }, r2.play = function() {
              this.paused = false;
              var e4 = this.video.buffered, t4 = this.video.currentTime;
              if (0 === e4.length) return this.logger.debug("Nothing buffered on play call"), void this.playInternal();
              this.logger.canLog("debug") && this.logger.debug("Play - currentTime " + this.video.currentTime + " buffer ranges " + B(this.video.buffered));
              var r3 = (function(e5, t5) {
                for (var r4 = 0; r4 < e5.length; r4++) {
                  var n2 = e5.start(r4), i2 = e5.end(r4);
                  if (!(i2 - n2 < b)) {
                    if (t5 < n2) return n2;
                    if (t5 >= n2 && t5 <= i2 - b) return t5;
                  }
                }
                return t5;
              })(e4, t4);
              r3 !== t4 && (this.logger.warn("Play - moving to buffered region", r3, t4), this.onGapSkipped(r3 - t4), this.seekTo(r3)), this.playInternal();
            }, r2.endOfStream = function() {
              this.idle = false, clearTimeout(this.idleTimeout), this.idleTimeout = -1;
            }, r2.seekTo = function(e4) {
              this.video.seekable.length ? e4 !== this.video.currentTime ? (this.logger.debug("Seeking from " + this.video.currentTime + " to " + e4), this.seeking = true, this.video.currentTime = e4) : this.logger.debug("Seek called at current time") : this.logger.debug("Seek called, but no seekable ranges");
            }, r2.addSourceBuffer = function(e4, t4) {
              (t4.indexOf("mp4a") > -1 || t4.indexOf("opus") > -1) && (this.audioBufferList = [e4]);
            }, r2.clearSourceBuffers = function() {
              this.audioBufferList = [];
            }, r2.onIdle = function() {
              this.listener.onSinkIdle();
            }, r2.onBuffering = function() {
              var e4;
              this.listener.onSinkBuffering(), null == (e4 = this.onSinkBuffering) || e4.call(this);
            }, r2.onVideoPlay = function() {
              var e4 = this;
              this.playAttempt ? (this.logger.debug("onVideoPlay - handling Core play call"), this.lastPlayhead = this.video.currentTime, clearInterval(this.intervalId), this.intervalId = self.setInterval(function() {
                return e4.heartbeat();
              }, T)) : (this.logger.debug("onVideoPlay - handling native play call"), this.pause(), this.listener.play()), this.playAttempt = false;
            }, r2.onVideoPause = function() {
              this.logger.debug("onVideoPause - awaitingAutoplayCompletion: " + this.awaitingAutoplayCompletion), this.awaitingAutoplayCompletion || (this.logger.debug("onVideoPause handled, clearing heartbeatInterval and stalledPlayheadTimeout"), this.checkStopped(false), clearInterval(this.intervalId));
            }, r2.onVideoTimeUpdate = function() {
              this.logger.canLog("debug") && this.logger.debug("Time update\n" + this.getPlayheadDebugLogString()), clearTimeout(this.idleTimeout), this.idleTimeout = -1, this.listener.onSinkTimeUpdate();
              var e4 = N(this.video.buffered, this.video.currentTime, b);
              this.checkBufferUpdate(e4), this.updateIdle(e4);
            }, r2.onVideoEnded = function() {
              this.listener.onSinkEnded();
            }, r2.onVideoPlaying = function() {
              this.logger.debug("onVideoPlaying - video.paused: " + this.video.paused), this.video.paused || this.listener.onSinkPlaying(this.paused);
            }, r2.onVideoSeeking = function() {
              if (this.seeking) return this.logger.debug("onVideoSeeking - already in a seek"), void (this.seeking = false);
              this.logger.debug("onVideoSeeking - seeking to " + this.video.currentTime), this.listener.seekTo(this.video.currentTime);
            }, r2.onVideoError = function() {
              var e4, t4, r3 = null != (e4 = null == this || null == (t4 = this.video) ? void 0 : t4.error) ? e4 : {}, n2 = r3.code, i2 = void 0 === n2 ? -1 : n2, o2 = r3.message, a2 = void 0 === o2 ? "" : o2;
              this.listener.onSinkError({ value: i2, code: i2, message: a2 });
            }, r2.heartbeat = function() {
              var e4 = N(this.video.buffered, this.video.currentTime, b);
              if (this.video.paused) clearInterval(this.intervalId);
              else if (this.video.currentTime === this.lastPlayhead) {
                var t4 = F(this.video.buffered, this.video.currentTime, b);
                this.logger.debug("Heartbeat - playhead stall detected, currentPosition " + this.video.currentTime + " nextPlayablePosition " + t4), t4 !== this.video.currentTime && (this.audioBufferList.map(function(e5) {
                  O("Audio Buffer", e5.buffered);
                }), O("<video> Buffer", this.video.buffered), console.warn("jumping " + (t4 - this.video.currentTime) + "s gap, current position " + this.video.currentTime + ", new position " + t4), this.onGapSkipped(t4 - this.video.currentTime), this.seekTo(t4)), this.updateIdle(e4);
              } else this.logger.debug("Heartbeat - playhead not stalled"), this.checkBufferUpdate(e4), this.lastPlayhead = this.video.currentTime;
            }, r2.checkBufferUpdate = function(e4) {
              var t4 = e4.end;
              t4 !== this.lastBufferEnd && (this.lastBufferEnd = t4, this.listener.onSinkBufferUpdate());
            }, r2.updateIdle = function(e4) {
              var t4 = this, r3 = e4.end, n2 = void 0 === r3 ? 0 : r3, i2 = this.video, o2 = i2.currentTime, a2 = i2.paused;
              if (a2 && !this.idle) this.idle = true, this.onIdle();
              else if (!a2) {
                var s2 = n2 - o2 < b;
                s2 && !this.idle && (console.warn("Playhead stalling at " + o2 + ", buffer end " + n2), clearTimeout(this.idleTimeout), this.idleTimeout = self.setTimeout(function() {
                  return t4.onBufferingTimeout();
                }, S), this.onBuffering()), this.idle = s2;
              }
            }, r2.updateConfig = function(e4) {
              this.config = f()({}, this.config, e4), this.logger.info("Config changed", this.config);
            }, r2.onBufferingTimeout = function() {
              clearTimeout(this.idleTimeout), this.idleTimeout = -1, this.listener.onSinkError({ value: y, code: y, message: "Buffering timeout" });
            }, r2.playInternal = function() {
              var e4 = this;
              this.logger.debug("playInternal"), this.playAttempt = true, this.awaitingAutoplayCompletion = true, Hr.resolve(this.video.play()).then(function() {
                e4.logger.debug("Play promise resolved"), e4.awaitingAutoplayCompletion = false;
              }).catch(function() {
                e4.logger.debug("Play promise rejected"), e4.playAttempt = false, e4.checkStopped(true);
              });
            }, r2.getPlayheadDebugLogString = function() {
              var e4 = N(this.video.buffered, this.video.currentTime, b);
              return "current buffer start: " + e4.start + "\ncurrent time: " + this.video.currentTime + "\ncurrent buffer end: " + e4.end + "\nbuffer ranges: " + B(this.video.buffered) + "\nlastPlayhead: " + this.lastPlayhead + "\nlastBufferEnd: " + this.lastBufferEnd + "\nvideo.paused: " + this.video.paused + "\nthis.paused: " + this.paused + "\nthis.idle: " + this.idle + "\nthis.seeking: " + this.seeking;
            }, t3;
          })(Wr), zr = z("mse-media-element-sink").configureLogger, qr = (function(e3) {
            function t3(t4, r3) {
              var n2;
              return (n2 = e3.call(this) || this).listener = t4, n2.video = r3, n2.playbackMonitor = void 0, n2.controlsObserver = void 0, n2.playbackMonitor = new Kr(r3, t4, {}), n2.observeControlsChange(), n2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.configure = function(e4) {
              var t4 = e4.srcObj;
              zr.debug("configure called", { hasSrcObject: !!t4 }), this.video.srcObject || (this.video.srcObject = t4);
            }, r2.invoke = function(e4) {
              this[e4.name].call(this, e4.arg);
            }, r2.play = function() {
              this.playbackMonitor.play();
            }, r2.pause = function() {
              this.playbackMonitor.pause();
            }, r2.seekTo = function(e4) {
              this.playbackMonitor.seekTo(e4);
            }, r2.endOfStream = function() {
              this.playbackMonitor.endOfStream();
            }, r2.setVolume = function(e4) {
              this.video.volume !== e4 && (this.video.volume = e4);
            }, r2.getVolume = function() {
              return this.video.volume;
            }, r2.isMuted = function() {
              return this.video.muted;
            }, r2.setMuted = function(e4) {
              this.video.muted !== e4 && (this.video.muted = e4);
            }, r2.setPlaybackRate = function(e4) {
              this.playbackMonitor.setPlaybackRate(e4);
            }, r2.getPlaybackRate = function() {
              return this.video.playbackRate;
            }, r2.getCurrentTime = function() {
              return this.video.currentTime;
            }, r2.getGapSkipStatistics = function() {
              return this.playbackMonitor.getGapSkipStatistics();
            }, r2.buffered = function() {
              return N(this.video.buffered, this.video.currentTime, b);
            }, r2.getBufferedRanges = function(e4) {
              return G(this.video.buffered);
            }, r2.bufferDuration = function() {
              var e4 = this.buffered(), t4 = e4.start;
              return e4.end - Math.max(t4, this.video.currentTime);
            }, r2.captureGesture = function() {
              this.playbackMonitor.play(), this.playbackMonitor.pause();
            }, r2.changeSrcObj = function(e4) {
              var t4 = this.video, r3 = t4.playbackRate;
              t4.srcObject = e4, t4.playbackRate = r3;
            }, r2.delete = function() {
              var e4;
              this.playbackMonitor.delete(), this.video.srcObject = null, V(this.video), null == (e4 = this.controlsObserver) || e4.disconnect();
            }, r2.onPlayerConfigurationChanged = function(e4) {
              var t4;
              null == (t4 = this.playbackMonitor) || t4.updateConfig({}), e4.media.preferManagedMediaSource && (this.video.disableRemotePlayback = true);
            }, r2.observeControlsChange = function() {
              var e4 = this.listener, t4 = this.video;
              try {
                (this.controlsObserver = new MutationObserver(function() {
                  e4.onSinkControlsChanged(t4.controls);
                })).observe(t4, { attributeFilter: ["controls"] }), e4.onSinkControlsChanged(t4.controls);
              } catch (e5) {
              }
            }, t3;
          })(Or), Qr = z("mse-media-sink"), Yr = Qr.configureLogger, Zr = Qr.rebuildLogger, Xr = (function() {
            function e3(e4, t4, r2) {
              void 0 === r2 && (r2 = new Kr(t4, e4, {})), this.listener = e4, this.video = t4, this.playbackMonitor = r2, this.controlsObserver = void 0, this.mseSink = void 0, this.awaitSink = void 0, this.playerConfig = void 0, this.sinkRebuildOnDiscontinuity = false, this.observeControlsChange(), this.awaitSink = void 0, U(t4, "error", this.onVideoError.bind(this));
            }
            var t3 = e3.prototype;
            return t3.invoke = function(e4) {
              var t4 = this.awaitSink, r2 = this.mseSink;
              t4 && r2 ? ["enqueue", "addTrack", "setTimestampOffset"].includes(e4.name) ? this.invokeAsync(e4) : this.invokeSync(e4) : t4 ? this.invokeAsync(e4) : r2 && this.invokeSync(e4);
            }, t3.initSink = function(e4, t4) {
              var r2 = this, n2 = this.awaitSink;
              if (!this.mseSink && !n2) {
                var i2;
                Yr.info("creating initial sink");
                var o2 = Z.create(this.onMediaSourceEnded.bind(this), this.onMediaSourceError.bind(this), null == (i2 = this.playerConfig) ? void 0 : i2.media);
                this.awaitSink = new Hr(function(n3, i3) {
                  o2.sink.then(function(i4) {
                    i4.setExpectedTracks(e4), r2.handleCreateSuccess(i4), r2.onSourceDurationChanged(t4), n3();
                  }).catch(function(e5) {
                    r2.handleCreateError(e5), i3();
                  });
                }), this.changeSrc(URL.createObjectURL(o2.ms));
              }
            }, t3.configure = function(e4) {
              var t4, r2, n2, i2 = e4.expectedTracks, o2 = e4.duration, a2 = e4.configurationDetails;
              this.initSink(i2, o2), Yr.debug("configure called", { trackID: e4.trackID, codec: e4.codec, mode: e4.mode, isProtected: e4.isProtected, expectedTracks: i2 });
              var s2 = (null != (t4 = null == (r2 = this.mseSink) ? void 0 : r2.getBufferedRanges("video").length) ? t4 : 0) > 0 && this.getSinkRebuildOnDiscontinuity() && a2.manifestDiscontinuityPresent && !a2.inSkippableAd && !a2.adCreativeTransition, u2 = true !== (null == (n2 = this.mseSink) ? void 0 : n2.isTrackCompatible(e4));
              this.awaitSink || !u2 && !s2 || (Zr.info("rebuild queued", { rebuildOnIncompatibleTrack: u2, rebuildOnDiscontinuity: s2 }), this.queueNewSink(i2, o2)), this.invoke({ name: "addTrack", arg: f()({}, Jr, e4) });
            }, t3.queueNewSink = function(e4, t4) {
              var r2 = this;
              Zr.info("queueNewSink started", { trackCount: e4, duration: t4 }), this.awaitSink = new Hr(function(n2, i2) {
                r2.deferUntilBuffering().then(function() {
                  var e5, t5 = Z.create(r2.onMediaSourceEnded.bind(r2), r2.onMediaSourceError.bind(r2), null == (e5 = r2.playerConfig) ? void 0 : e5.media);
                  return r2.changeSrc(URL.createObjectURL(t5.ms)), t5.sink;
                }).then(function(i3) {
                  i3.setExpectedTracks(e4), r2.destroyMSESink(), r2.handleCreateSuccess(i3), r2.onSourceDurationChanged(t4), r2.play(), Zr.info("queueNewSink resolved"), n2();
                }).catch(function(e5) {
                  Zr.warn("queueNewSink failed", e5), r2.handleCreateError(e5), i2();
                });
              });
            }, t3.addTrack = function(e4) {
              var t4 = e4.trackID, r2 = e4.codec, n2 = e4.group, i2 = e4.isProtected, o2 = this.mseSink;
              try {
                var a2 = null == o2 ? void 0 : o2.addTrack(t4, r2, n2, i2);
                a2 && this.playbackMonitor.addSourceBuffer(a2, r2);
              } catch (e5) {
                this.handleCreateError(e5);
              }
            }, t3.enqueue = function(e4) {
              var t4, r2 = e4.trackID, n2 = e4.buffer;
              null == (t4 = this.mseSink) || t4.append(r2, n2);
            }, t3.endOfStream = function() {
              var e4, t4 = this;
              null == (e4 = this.mseSink) || e4.scheduleUpdate().then(function() {
                return t4.playbackMonitor.endOfStream();
              });
            }, t3.setTimestampOffset = function(e4) {
              var t4, r2 = e4.trackID, n2 = e4.offset;
              null == (t4 = this.mseSink) || t4.setTimestampOffset(r2, n2);
            }, t3.onSourceDurationChanged = function(e4) {
              var t4;
              null == (t4 = this.mseSink) || t4.updateDuration(e4, this.video.controls);
            }, t3.setSinkRebuildOnDiscontinuity = function() {
              this.sinkRebuildOnDiscontinuity = true;
            }, t3.getSinkRebuildOnDiscontinuity = function() {
              return this.sinkRebuildOnDiscontinuity;
            }, t3.onPlayerConfigurationChanged = function(e4) {
              var t4, r2, n2;
              this.playerConfig = e4, e4.media.preferManagedMediaSource && (this.video.disableRemotePlayback = true), null == (t4 = this.mseSink) || t4.setSupportsChangeType(e4.media.supportsMixedCodec), null != (r2 = e4.media) && r2.supportsCodecProfileTransition || this.setSinkRebuildOnDiscontinuity(), null == (n2 = this.playbackMonitor) || n2.updateConfig({});
            }, t3.play = function() {
              var e4, t4 = this;
              null == (e4 = this.mseSink) || e4.scheduleUpdate().then(function() {
                return t4.playbackMonitor.play();
              });
            }, t3.pause = function() {
              var e4, t4 = this;
              null == (e4 = this.mseSink) || e4.scheduleUpdate().then(function() {
                return t4.playbackMonitor.pause();
              });
            }, t3.remove = function(e4) {
              var t4, r2 = e4.start, n2 = e4.end;
              null == (t4 = this.mseSink) || t4.remove(r2, n2);
            }, t3.seekTo = function(e4) {
              var t4 = this.mseSink, r2 = this.playbackMonitor, n2 = this.video, i2 = N(n2.buffered, n2.currentTime, b), o2 = i2.start, a2 = i2.end;
              e4 >= o2 && e4 < a2 ? null == t4 || t4.scheduleUpdate().then(function() {
                return r2.seekTo(e4);
              }) : r2.seekTo(e4);
            }, t3.setVolume = function(e4) {
              this.video.volume !== e4 && (this.video.volume = e4);
            }, t3.getVolume = function() {
              return this.video.volume;
            }, t3.isMuted = function() {
              return this.video.muted;
            }, t3.setMuted = function(e4) {
              this.video.muted !== e4 && (this.video.muted = e4);
            }, t3.setPlaybackRate = function(e4) {
              this.playbackMonitor.setPlaybackRate(e4);
            }, t3.getPlaybackRate = function() {
              return this.video.playbackRate;
            }, t3.getCurrentTime = function() {
              return this.video.currentTime;
            }, t3.buffered = function() {
              return N(this.video.buffered, this.video.currentTime, b);
            }, t3.getBufferedRanges = function(e4) {
              var t4, r2;
              return null != (t4 = null == (r2 = this.mseSink) ? void 0 : r2.getBufferedRanges(e4)) ? t4 : [];
            }, t3.bufferDuration = function() {
              var e4 = this.buffered(), t4 = e4.start;
              return e4.end - Math.max(t4, this.video.currentTime);
            }, t3.captureGesture = function() {
              this.playbackMonitor.play(), this.playbackMonitor.pause();
            }, t3.changeSrc = function(e4) {
              !(function(e5, t4) {
                var r2 = e5.playbackRate, n2 = e5.src;
                n2 && URL.revokeObjectURL(n2), e5.src = t4, e5.playbackRate = r2;
              })(this.video, e4);
            }, t3.changeSrcObj = function(e4) {
            }, t3.delete = function() {
              var e4;
              this.playbackMonitor.delete(), null == (e4 = this.controlsObserver) || e4.disconnect(), this.destroyMSESink(), V(this.video);
            }, t3.invokeSync = function(e4) {
              this[e4.name].call(this, e4.arg);
            }, t3.invokeAsync = function(e4) {
              var t4, r2 = this;
              null == (t4 = this.awaitSink) || t4.then(function() {
                return r2.invokeSync(e4);
              }).catch(function() {
              });
            }, t3.onMediaSourceEnded = function() {
              this.video.load(), this.listener.onSinkReset();
            }, t3.destroyMSESink = function() {
              var e4 = this, t4 = function() {
                e4.mseSink && e4.mseSink.destroy(), e4.awaitSink = void 0, e4.mseSink = void 0;
              };
              this.mseSink ? t4() : this.awaitSink && this.awaitSink.then(function() {
                return t4();
              }), this.playbackMonitor && this.playbackMonitor.clearSourceBuffers();
            }, t3.deferUntilBuffering = function() {
              var e4 = this, t4 = this.mseSink, r2 = this.playbackMonitor;
              return new Hr(function(n2) {
                t4 && !e4.video.paused ? r2.onSinkBuffering = function() {
                  r2.onSinkBuffering = void 0, n2();
                } : n2();
              });
            }, t3.handleCreateSuccess = function(e4) {
              this.mseSink = e4, this.awaitSink = void 0, this.playerConfig && this.mseSink.setSupportsChangeType(this.playerConfig.media.supportsMixedCodec), this.mseSink.setLiveSeekableRange(0, E), this.listener.onSinkCreated({ isMMS: this.mseSink.getMediaSourceInfo().isManagedMediaSource, isWorker: false });
            }, t3.handleCreateError = function(e4) {
              this.listener.onSinkError({ value: 4, code: 4, message: e4.toString() });
            }, t3.onMediaSourceError = function(e4, t4, r2, n2) {
              var i2 = { value: e4, code: t4, message: r2 };
              n2 ? this.listener.onSinkError(i2) : this.listener.onSinkRecoverableError(i2);
            }, t3.onVideoError = function() {
              this.destroyMSESink();
            }, t3.observeControlsChange = function() {
              var e4 = this, t4 = this.video;
              try {
                (this.controlsObserver = new MutationObserver(function() {
                  e4.invoke({ name: "onSourceDurationChanged", arg: t4.duration });
                })).observe(t4, { attributeFilter: ["controls"] });
              } catch (e5) {
              }
            }, t3.onSegmentDiscontinuity = function() {
            }, t3.getGapSkipStatistics = function() {
              return this.playbackMonitor.getGapSkipStatistics();
            }, e3;
          })(), Jr = { trackID: 0, codec: 'codecs="magic"', mode: "mse", isProtected: false, path: "", group: "", srcObj: null, expectedTracks: 1, duration: 1 / 0, configurationDetails: { manifestDiscontinuityPresent: false, inSkippableAd: false, adCreativeTransition: false } }, $r = (function(e3) {
            return e3.Live_Latency_Load = "liveLatencyLoad", e3.Live_Latency_Stats = "liveLatencyStats", e3;
          })({}), en = (function(e3) {
            return e3.Trigger_Drift_Correction = "triggerDriftCorrection", e3;
          })({}), tn = (function() {
            function e3() {
              this.serverOffsetVal = e3.SERVER_OFFSET_DEFAULT, this.liveLatencyVal = 0, this.lastTranscodeReceive = -1;
            }
            var t3 = e3.prototype;
            return t3.tryGenerateServerOffset = function(t4) {
              return this.serverOffsetVal !== e3.SERVER_OFFSET_DEFAULT ? (console.warn("[generateServerOffset] the server offset has already been generated, skipping"), false) : (this.serverOffsetVal = (function(e4, t5) {
                return new Date(1e3 * t5).getTime() - e4;
              })(Date.now(), t4), true);
            }, t3.tryUpdateLatency = function(t4) {
              return this.serverOffsetVal !== e3.SERVER_OFFSET_DEFAULT && (t4 < this.lastTranscodeReceive ? (console.warn("[updateLatency] received latency values too old, ignoring. previous: " + this.lastTranscodeReceive + " current: " + t4), false) : (this.liveLatencyVal = (function(e4, t5, r2) {
                return (e4 + t5 - r2) / 1e3;
              })(Date.now(), this.serverOffsetVal, t4), this.lastTranscodeReceive = t4, true));
            }, p()(e3, [{ key: "serverOffset", get: function() {
              return this.serverOffsetVal;
            } }, { key: "liveLatency", get: function() {
              return this.liveLatencyVal;
            } }]);
          })();
          tn.SERVER_OFFSET_DEFAULT = -1;
          var rn = function(e3) {
            var t3 = parseFloat(e3);
            if (!isNaN(t3)) return t3;
          }, nn = (function(e3) {
            return e3.ID3 = "org.id3", e3.APPLE_HLS = "com.apple.quicktime.HLS", e3;
          })(nn || {}), on = (function(e3) {
            return e3.PRIV = "PRIV", e3.TXXX = "TXXX", e3.TDEN = "TDEN", e3.STREAM_LEVEL_SERVER_TIME = "X-SERVER-TIME", e3;
          })(on || {}), an = z("passthrough-sink"), sn = an.logger, un = an.configureLogger, cn = (function(e3) {
            function t3(t4, r3) {
              var n2;
              (n2 = e3.call(this, r3, t4) || this).intervalId = void 0, n2.bufferingTimeoutId = void 0, n2.attemptingToPlay = void 0, n2.hasPlayedSrc = void 0, n2.hasReloadedOnDecodeError = void 0, n2.unsubscribersForTrackEvents = void 0, n2.latencyStatistics = void 0, n2.processedInitialCues = void 0, n2.serviceWorker = void 0, n2.serviceWorkerHandler = void 0, n2.positionTracker = void 0, n2.driftDetector = void 0, n2.driftDetectorHandler = void 0, n2.analytics = void 0, n2.analyticsHandler = void 0, n2.intervalId = -1, n2.bufferingTimeoutId = -1, n2.attemptingToPlay = false, n2.hasPlayedSrc = false, n2.hasReloadedOnDecodeError = false, n2.unsubscribersForTrackEvents = [], n2.latencyStatistics = new tn(), n2.processedInitialCues = false, n2.addListener("waiting", function() {
                return n2.onVideoWaiting();
              }, n2.video), n2.addListener("timeupdate", function() {
                return n2.onVideoTimeUpdate();
              }, n2.video), n2.addListener("durationchange", function() {
                return n2.onVideoDurationChange();
              }, n2.video), n2.addListener("error", function() {
                return n2.onVideoError();
              }, n2.video), n2.addListener("play", function() {
                return n2.onVideoPlay();
              }, n2.video), n2.addListener("pause", function() {
                return n2.onVideoPause();
              }, n2.video), n2.addListener("ended", function() {
                return n2.onVideoEnded();
              }, n2.video), n2.addListener("playing", function() {
                return n2.onVideoPlaying();
              }, n2.video);
              var i2, o2, a2, s2, u2, c2, l2, d2, h2, p2, v2, g2, m2, y2, b2 = A(document).visibilityChange;
              if (n2.addListener(b2, function() {
                return n2.onVisibilityChange();
              }, document), n2.serviceWorker = lr(), n2.serviceWorkerHandler = function(e4) {
                return n2.onServiceWorkerMessage(e4);
              }, n2.addServiceWorkerListeners(), void 0 !== n2.serviceWorker) {
                var E2;
                n2.positionTracker = (v2 = [], g2 = -1, m2 = function() {
                  return 0 === v2.length || void 0 === p2 ? (g2 = -1, false) : -1 !== (g2 = y2(p2));
                }, y2 = function(e4) {
                  var t5 = new RegExp("^" + e4);
                  if (v2) {
                    var r4 = v2.findIndex(function(e5) {
                      return t5.test(e5.pdt);
                    });
                    if (-1 === r4) for (var n3 = /* @__PURE__ */ new Date(e4 + "Z"), i3 = 0; i3 < v2.length; i3++) {
                      var o3 = v2[i3], a3 = new Date(o3.pdt);
                      if (a3.getTime() <= n3.getTime() && a3.getTime() + 1e3 * o3.duration > n3.getTime()) {
                        r4 = i3;
                        break;
                      }
                    }
                    if (-1 !== r4) return r4;
                  }
                  return console.warn("Couldn't find segment for PDT " + e4), -1;
                }, { processAvailableSegments: function(e4) {
                  return v2 = e4, m2();
                }, processTDEN: function(e4) {
                  return p2 = e4, m2();
                }, getCurrentPosition: function() {
                  return g2;
                } });
                var S2 = null == (E2 = n2.serviceWorker) ? void 0 : E2.getDriftDetectionConfig();
                void 0 !== S2 && S2.enabled && (n2.driftDetector = (i2 = { maxPlaylistPosition: S2.maxPlaylistPosition, triggerDetectionThreshold: S2.triggerDetectionThreshold, minDurationBetweenCorrections: S2.minDurationBetweenCorrections }, u2 = new tr(), c2 = 0, l2 = 0, d2 = function() {
                  if (void 0 !== o2 && void 0 !== a2) {
                    if (o2 <= i2.maxPlaylistPosition) l2 = 0;
                    else if ((l2 += 1) > i2.triggerDetectionThreshold) {
                      console.debug("[handleDriftDetection] above detection threshold");
                      var e4 = Date.now();
                      (void 0 === s2 || e4 - s2 > i2.minDurationBetweenCorrections) && (console.debug("[handleDriftDetection] within min duration, triggering rebuffer-to-live"), h2(e4, o2, a2));
                    }
                  }
                }, h2 = function(e4, t5, r4) {
                  c2 += 1, s2 = e4, l2 = 0;
                  var n3 = { type: en.Trigger_Drift_Correction, playlistPosition: t5, playhead: r4, rebufferCount: c2 };
                  u2.emit(en.Trigger_Drift_Correction, n3);
                }, { addListener: function(e4, t5) {
                  u2.on(e4, t5);
                }, removeListener: function(e4, t5) {
                  u2.removeListener(e4, t5);
                }, processPlaylistPosition: function(e4) {
                  o2 = e4;
                }, processTimeUpdate: function(e4) {
                  var t5 = Math.floor(e4);
                  void 0 !== a2 && t5 === a2 || (a2 = t5, d2());
                } }), n2.driftDetectorHandler = function(e4) {
                  return n2.onDriftDetectorEvent(e4);
                }, n2.addDriftDetectorListeners()), n2.analytics = (function() {
                  var e4, t5, r4, n3, i3 = new tr(), o3 = {}, a3 = { duration: 0 }, s3 = { min: 0, avg: 0, max: 0, last: 0 }, u3 = { first: void 0, last: void 0 }, c3 = { count: 0 }, l3 = function() {
                    if ((r5 = $r.Live_Latency_Load) === $r.Live_Latency_Load && void 0 === o3[r5] && void 0 !== e4 && void 0 !== t5) {
                      var r5, n4 = o3[$r.Live_Latency_Load] || 0;
                      o3[$r.Live_Latency_Load] = n4 + 1, i3.emit($r.Live_Latency_Load, { type: $r.Live_Latency_Load, serverOffset: e4, manifestLoadAnalytics: t5 });
                    }
                  }, d3 = function() {
                    var e5 = o3[$r.Live_Latency_Stats] || 0;
                    o3[$r.Live_Latency_Stats] = e5 + 1, i3.emit($r.Live_Latency_Stats, { type: $r.Live_Latency_Stats, playlistPosition: f()({}, s3), liveLatencyTiming: f()({}, u3), driftCorrection: f()({}, c3) });
                  };
                  return { addListener: function(e5, t6) {
                    i3.on(e5, t6);
                  }, removeListener: function(e5, t6) {
                    i3.removeListener(e5, t6);
                  }, processServerOffset: function(t6) {
                    e4 = { serverTime: 1e3 * t6.serverTime, clientTime: t6.clientTime, serverOffset: t6.serverOffset }, l3();
                  }, processManifestLoadAnalytics: function(e5) {
                    t5 = e5, l3();
                  }, processSinkState: function(e5) {
                    "playing" === r4 && "pause" === e5 && d3(), r4 = e5;
                  }, processTimeUpdate: function(e5) {
                    var t6 = Math.floor(e5);
                    void 0 !== n3 && t6 === n3 || (a3.duration += 1, n3 = t6, a3.duration % 10 == 0 && d3());
                  }, processPlaylistPosition: function(e5) {
                    e5 < s3.min && (s3.min = e5), e5 > s3.max && (s3.max = e5), 0 === s3.avg ? s3.avg = e5 : s3.avg = (s3.avg * a3.duration + e5) / (a3.duration + 1), s3.last = e5;
                  }, processLiveLatencyUpdate: function(e5) {
                    void 0 === u3.first && (u3.first = f()({}, e5)), u3.last = f()({}, e5);
                  }, processDriftCorrectionStart: function() {
                    c3.count += 1;
                  } };
                })(), n2.analyticsHandler = function(e4) {
                  return n2.onAnalyticsEvent(e4);
                }, n2.addAnalyticsListeners();
              }
              return n2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.invoke = function(e4) {
              this[e4.name].call(this, e4.arg);
            }, r2.configure = function(e4) {
              var t4 = e4.path;
              un.debug("configure called", { path: t4 }), this.handleTrackEvents(), this.hasReloadedOnDecodeError = false, this.hasPlayedSrc = false, this.video.src = t4;
            }, r2.play = function() {
              var e4 = this, t4 = this.video.buffered;
              if (t4.length > 0) {
                var r3 = t4.start(t4.length - 1), n2 = t4.end(t4.length - 1);
                this.video.duration === 1 / 0 && (n2 < this.video.currentTime || this.video.currentTime < r3) && (sn.warn("Moving to buffered region"), this.onGapSkipped(r3 - this.video.currentTime), this.video.currentTime = r3);
              }
              this.paused = false, this.attemptingToPlay = true, (void 0 === e4.serviceWorker || e4.hasPlayedSrc ? Promise.resolve(e4.video.play()) : e4.serviceWorker.ensureConfigured().catch(function(e5) {
                sn.warn("sw, ensureConfigured failed", e5);
              }).then(function() {
                return Promise.resolve(e4.video.play());
              })).then(function() {
                e4.attemptingToPlay = false, e4.hasPlayedSrc = true;
              }).catch(function() {
                e4.attemptingToPlay = false, e4.checkStopped(true);
              });
            }, r2.pause = function() {
              e3.prototype.pause.call(this), clearTimeout(this.intervalId);
            }, r2.seekTo = function(e4) {
              this.video.currentTime = e4;
            }, r2.setVolume = function(e4) {
              this.video.volume = e4;
            }, r2.getVolume = function() {
              return this.video.volume;
            }, r2.buffered = function() {
              return N(this.video.buffered, this.video.currentTime, b);
            }, r2.getBufferedRanges = function(e4) {
              return G(this.video.buffered);
            }, r2.decodedFrames = function() {
              return R(this.video);
            }, r2.droppedFrames = function() {
              return L(this.video);
            }, r2.delete = function() {
              e3.prototype.delete.call(this), this.removeTrackListeners(), this.removeServiceWorkerListeners(), this.removeDriftDetectorListeners(), this.removeAnalyticsListeners(), V(this.video);
            }, r2.isMuted = function() {
              return this.video.muted;
            }, r2.setMuted = function(e4) {
              this.video.muted = e4;
            }, r2.getPlaybackRate = function() {
              return this.video.playbackRate;
            }, r2.getCurrentTime = function() {
              return this.video.currentTime;
            }, r2.bufferDuration = function() {
              var e4 = this.buffered(), t4 = e4.start;
              return e4.end - Math.max(t4, this.video.currentTime);
            }, r2.captureGesture = function() {
              Promise.resolve(this.video.play()).catch(function() {
              }), this.video.pause();
            }, r2.addServiceWorkerListeners = function() {
              void 0 !== this.serviceWorker && (this.serviceWorker.addListener(ir.Manifest_Load_Analytics, this.serviceWorkerHandler), this.serviceWorker.addListener(ir.Available_Segments, this.serviceWorkerHandler));
            }, r2.onServiceWorkerMessage = function(e4) {
              switch (e4.type) {
                case ir.Manifest_Load_Analytics:
                  var t4;
                  null == (t4 = this.analytics) || t4.processManifestLoadAnalytics(e4.data);
                  break;
                case ir.Available_Segments:
                  var r3;
                  null == (r3 = this.positionTracker) || r3.processAvailableSegments(e4.segments);
              }
            }, r2.removeServiceWorkerListeners = function() {
              void 0 !== this.serviceWorker && (this.serviceWorker.removeListener(ir.Manifest_Load_Analytics, this.serviceWorkerHandler), this.serviceWorker.removeListener(ir.Available_Segments, this.serviceWorkerHandler));
            }, r2.addDriftDetectorListeners = function() {
              var e4;
              null == (e4 = this.driftDetector) || e4.addListener(en.Trigger_Drift_Correction, this.driftDetectorHandler);
            }, r2.onDriftDetectorEvent = function(e4) {
              var t4;
              e4.type === en.Trigger_Drift_Correction && (sn.debug("triggering drift correction", e4), null == (t4 = this.analytics) || t4.processDriftCorrectionStart(), this.resumeAtLive());
            }, r2.resumeAtLive = function() {
              this.pause(), this.configure({ path: this.video.src }), this.play();
            }, r2.removeDriftDetectorListeners = function() {
              var e4;
              null == (e4 = this.driftDetector) || e4.removeListener(en.Trigger_Drift_Correction, this.driftDetectorHandler);
            }, r2.addAnalyticsListeners = function() {
              var e4, t4;
              null == (e4 = this.analytics) || e4.addListener($r.Live_Latency_Load, this.analyticsHandler), null == (t4 = this.analytics) || t4.addListener($r.Live_Latency_Stats, this.analyticsHandler);
            }, r2.onAnalyticsEvent = function(e4) {
              var t4;
              switch (null == (t4 = this.serviceWorker) || t4.debugPassthroughAnalytics(e4), e4.type) {
                case $r.Live_Latency_Load:
                  sn.debug("analytics event, Live Latency Load", e4.serverOffset, e4.manifestLoadAnalytics);
                  break;
                case $r.Live_Latency_Stats:
                  sn.debug("analytics event, Live Latency Stats", e4.playlistPosition, e4.liveLatencyTiming, e4.driftCorrection);
              }
            }, r2.removeAnalyticsListeners = function() {
              var e4, t4;
              null == (e4 = this.analytics) || e4.removeListener($r.Live_Latency_Load, this.analyticsHandler), null == (t4 = this.analytics) || t4.removeListener($r.Live_Latency_Stats, this.analyticsHandler);
            }, r2.addTrackListener = function(e4, t4, r3) {
              this.unsubscribersForTrackEvents.push(U(r3, e4, t4));
            }, r2.removeTrackListeners = function() {
              this.unsubscribersForTrackEvents.forEach(function(e4) {
                return e4();
              });
            }, r2.checkTracksStatus = function() {
              for (var e4 = this.video.textTracks, t4 = 0; t4 < e4.length; t4++) {
                var r3 = e4[t4];
                "metadata" === r3.kind && "disabled" === r3.mode && (r3.mode = "hidden");
              }
            }, r2.handleTDENDataReceived = function(e4) {
              if (void 0 !== this.positionTracker && this.positionTracker.processTDEN(e4)) {
                var t4, r3, n2 = this.positionTracker.getCurrentPosition();
                null == (t4 = this.analytics) || t4.processPlaylistPosition(n2), null == (r3 = this.driftDetector) || r3.processPlaylistPosition(n2);
              }
            }, r2.handleTXXXSegmentDataReceived = function(e4) {
              var t4, r3 = (function(e5) {
                var t5 = k(e5);
                if ("transc_r" in t5) return { transc_r: parseInt(t5.transc_r) };
              })(e4);
              void 0 !== r3 && (r3.transc_r > 0 && this.listener.onPassthroughSinkPropertyChanged("transcodeReceiveInSecs", r3.transc_r / 1e3), this.latencyStatistics.tryUpdateLatency(r3.transc_r) && (this.listener.onPassthroughSinkPropertyChanged("liveLatency", this.latencyStatistics.liveLatency), null == (t4 = this.analytics) || t4.processLiveLatencyUpdate({ transc_r: r3.transc_r, latency: this.latencyStatistics.liveLatency })));
            }, r2.handleInitialCues = function(e4) {
              for (var t4, r3, n2 = 0; n2 < e4.length; ++n2) {
                var i2 = e4[n2];
                i2.type === nn.APPLE_HLS && i2.value.key === on.STREAM_LEVEL_SERVER_TIME && (t4 = rn(i2.value.data));
              }
              this.processedInitialCues = true, void 0 !== t4 && this.latencyStatistics.tryGenerateServerOffset(t4) && (null == (r3 = this.analytics) || r3.processServerOffset({ serverTime: t4, clientTime: Date.now(), serverOffset: this.latencyStatistics.serverOffset }));
            }, r2.shouldPropagateCue = function(e4) {
              var t4 = e4.type, r3 = e4.value;
              return !(t4 !== nn.ID3 || !r3 || !(r3.key === on.TXXX && "segmentmetadata" !== r3.info || r3.key === on.PRIV && r3.info === s.METADATA_ID || r3.key === on.PRIV && r3.info === s.INBAND_METADATA_ID));
            }, r2.handleCueChange = function(e4) {
              var t4 = this, r3 = /* @__PURE__ */ new Set();
              this.addTrackListener("cuechange", function() {
                var n2;
                !t4.processedInitialCues && e4.cues && t4.handleInitialCues(e4.cues);
                var i2 = null != (n2 = e4.activeCues) ? n2 : [];
                if (i2.length > 0) {
                  for (var o2 = /* @__PURE__ */ new Set(), a2 = 0; a2 < i2.length; ++a2) {
                    var s2 = i2[a2];
                    if (!r3.has(s2)) {
                      var u2 = s2.value;
                      if (t4.listener.onPassthroughSinkDataCue(s2), u2.key === on.TXXX && "segmentmetadata" === u2.info && t4.handleTXXXSegmentDataReceived(u2.data), u2.key === on.TDEN && t4.handleTDENDataReceived(u2.data), t4.shouldPropagateCue(s2)) {
                        var c2 = u2.key === on.PRIV ? new TextDecoder("utf-8").decode(u2.data) : u2.data || "", l2 = u2.info || "";
                        t4.listener.onPassthroughSinkMetadata(s2.startTime, s2.endTime, c2, l2, l2);
                      }
                    }
                    o2.add(s2);
                  }
                  r3 = o2;
                }
              }, e4);
            }, r2.handleTrackEvents = function() {
              var e4 = this;
              this.removeTrackListeners(), void 0 === window.DataCue && void 0 === window.WebKitDataCue || (this.addTrackListener("change", function() {
                e4.checkTracksStatus();
              }, this.video.textTracks), this.addTrackListener("addtrack", function(t4) {
                var r3 = t4.track;
                "metadata" === r3.kind && "disabled" === r3.mode && (r3.mode = "hidden", e4.handleCueChange(r3));
              }, this.video.textTracks));
            }, r2.onVideoWaiting = function() {
              var e4 = this;
              if (N(this.video.buffered, this.video.currentTime, b).end - this.video.currentTime < b) {
                this.listener.onSinkBuffering(), clearTimeout(this.bufferingTimeoutId), this.bufferingTimeoutId = self.setTimeout(function() {
                  e4.listener.onSinkError({ value: y, code: y, message: "Buffering timeout" });
                }, S);
                var t4 = U(this.video, "timeupdate", function() {
                  t4(), clearTimeout(e4.bufferingTimeoutId);
                });
              }
              var r3 = U(this.video, "timeupdate", function() {
                4 === e4.video.readyState && (r3(), e4.onVideoPlaying());
              });
            }, r2.onVideoTimeUpdate = function() {
              var e4, t4;
              this.listener.onSinkTimeUpdate(), null == (e4 = this.analytics) || e4.processTimeUpdate(this.getCurrentTime()), null == (t4 = this.driftDetector) || t4.processTimeUpdate(this.getCurrentTime());
            }, r2.onVideoDurationChange = function() {
              this.listener.onSinkDurationChanged(this.video.duration);
            }, r2.onVideoError = function() {
              var e4 = this.video.error, t4 = e4.code, r3 = e4.message, n2 = void 0 === r3 ? "" : r3, i2 = -1 !== this.video.src.indexOf(".m3u8");
              if (4 === t4 && !this.hasPlayedSrc && i2) return clearTimeout(this.bufferingTimeoutId), void this.listener.onSinkError({ value: 404, code: 404, message: n2 });
              3 !== t4 || this.hasReloadedOnDecodeError ? this.listener.onSinkError({ value: t4, code: t4, message: n2 }) : this.hasReloadedOnDecodeError || (this.hasReloadedOnDecodeError = true, console.warn("Reload video element on MEDIA_ERR_DECODE 3"), this.video.load());
            }, r2.onVideoPlay = function() {
              var e4 = this, t4 = this.video.currentTime;
              clearTimeout(this.intervalId), this.intervalId = self.setTimeout(function() {
                return e4.heartbeat(t4);
              }, T);
            }, r2.onVideoPause = function() {
              var e4;
              clearTimeout(this.intervalId), this.attemptingToPlay || (null == (e4 = this.analytics) || e4.processSinkState("pause"), this.checkStopped(false));
            }, r2.onVideoEnded = function() {
              this.listener.onSinkEnded();
            }, r2.onVideoPlaying = function() {
              var e4;
              this.video.paused || (null == (e4 = this.analytics) || e4.processSinkState("playing"), this.listener.onSinkPlaying(this.paused), this.trackBufferUpdate(N(this.video.buffered, this.video.currentTime, b).end));
            }, r2.onVisibilityChange = function() {
              var e4 = A(document).hidden;
              document[e4] && (this.hasReloadedOnDecodeError = false);
            }, r2.heartbeat = function(e4) {
              var t4, r3, n2, i2, o2 = this, a2 = this.video.currentTime;
              if (a2 === e4) {
                if (t4 = this.video, r3 = b, i2 = N(t4.buffered, t4.currentTime, r3).end - (n2 = t4.currentTime), !(t4.ended || t4.duration - n2 < r3) && i2 < r3) return void this.listener.onSinkBuffering();
                var s2 = F(this.video.buffered, a2, b);
                s2 !== a2 && (console.warn("jumping " + (s2 - a2) + "s gap"), this.onGapSkipped(s2 - a2), this.video.currentTime = s2, a2 = this.video.currentTime);
              }
              this.intervalId = self.setTimeout(function() {
                return o2.heartbeat(a2);
              }, T);
            }, r2.trackBufferUpdate = function(e4) {
              var t4 = this, r3 = this.buffered().end;
              r3 !== e4 && this.listener.onSinkBufferUpdate(), this.listener.onPassthroughSinkPropertyChanged("bufferedPosition", r3);
              var n2 = U(this.video, "timeupdate", function() {
                n2(), t4.trackBufferUpdate(r3);
              });
            }, t3;
          })(Wr), ln = (function(e3) {
            function t3() {
              return e3.apply(this, arguments) || this;
            }
            return Lr()(t3, e3), t3.prototype.onBuffering = function() {
              this.onSinkBuffering ? this.onSinkBuffering() : this.listener.onSinkBuffering();
            }, t3;
          })(Kr), dn = z("webview-sink"), fn = dn.configureLogger, hn = dn.rebuildLogger, pn = (function(e3) {
            function t3(t4, r3, n2) {
              var i2;
              return (i2 = e3.call(this, t4, r3, new ln(r3, t4)) || this).listener = t4, i2.video = r3, i2.adjustments = n2, i2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.isChangingContentProtection = function(e4) {
              var t4, r3;
              return (null != (t4 = null == (r3 = this.mseSink) ? void 0 : r3.isDrmProtected()) && t4) !== e4.isProtected;
            }, r2.configure = function(e4) {
              var t4 = e4.expectedTracks, r3 = e4.duration;
              if (this.initSink(t4, r3), fn.debug("configure called", { trackID: e4.trackID, codec: e4.codec, mode: e4.mode, isProtected: e4.isProtected, expectedTracks: t4 }), this.isChangingContentProtection(e4) && (hn.info("rebuild queued", { reason: "isChangingContentProtection" }), this.queueNewSink(t4, r3)), this.isNewSinkNeeded(e4)) {
                hn.info("rebuild queued", { reason: "isNewSinkNeeded" }), this.queueNewSink(t4, r3);
                var n2 = this.getTrack(_.audio);
                n2 && this.invoke({ name: "addTrack", arg: n2 });
              }
              this.invoke({ name: "addTrack", arg: f()({}, vn, e4) });
            }, r2.queueNewSink = function(e4, t4) {
              var r3 = this;
              hn.info("queueNewSink started", { trackCount: e4, duration: t4 }), this.awaitSink = new Promise(function(n2, i2) {
                r3.deferUntilBuffering().then(function() {
                  var e5 = Z.create(r3.onMediaSourceEnded.bind(r3), r3.onMediaSourceError.bind(r3));
                  return r3.changeSrc(URL.createObjectURL(e5.ms)), e5.sink;
                }).then(function(i3) {
                  i3.setExpectedTracks(e4), r3.invokeAsync({ name: "play", arg: void 0 }), r3.destroyMSESink(), r3.handleCreateSuccess(i3), r3.onSourceDurationChanged(t4), hn.info("queueNewSink resolved"), n2();
                }).catch(function(e5) {
                  hn.warn("queueNewSink failed", e5), r3.handleCreateError(e5), i2();
                });
              });
            }, r2.deferUntilBuffering = function() {
              var e4 = this, t4 = this.mseSink, r3 = this.playbackMonitor;
              return new Promise(function(n2) {
                t4 && !e4.video.paused ? (r3.onSinkBuffering = function() {
                }, e4.video.addEventListener("waiting", function() {
                  r3.onSinkBuffering = void 0, n2();
                }, { once: true })) : n2();
              });
            }, r2.onSegmentDiscontinuity = function() {
              var e4 = this.adjustments, t4 = this.awaitSink, r3 = this.mseSink;
              e4.rebuildMediaSinkOnDiscontinuity && r3 && !t4 && this.queueNewSink(r3.getExpectedTracks(), r3.duration);
            }, r2.isSinkVideoSourceQualityChangeRequired = function(e4, t4) {
              return !!this.adjustments.rebuildMediaSinkOnSourceQualityChange && "chunked" === e4 != ("chunked" === t4);
            }, r2.isNewSinkNeeded = function(e4) {
              var t4 = this.adjustments, r3 = this.mseSink, n2 = this.awaitSink;
              if (e4.trackID !== _.video) return false;
              if (!t4.rebuildMediaSinkOnSourceQualityChange) return false;
              if (!r3 || n2) return false;
              var i2 = this.getTrack(_.video), o2 = this.getTrack(_.audio);
              return !(!i2 || !o2) && this.isSinkVideoSourceQualityChangeRequired(i2.group, e4.group);
            }, r2.getTrack = function(e4) {
              var t4, r3 = this.mseSink;
              return null != (t4 = null == r3 ? void 0 : r3.bufferProperties.find(function(t5) {
                return t5.trackID === e4;
              })) ? t4 : null;
            }, t3;
          })(Xr), vn = { trackID: 0, codec: 'codecs="magic"', mode: "mse", isProtected: false, path: "", group: "", srcObj: null, expectedTracks: 1, duration: 1 / 0, configurationDetails: { manifestDiscontinuityPresent: false, inSkippableAd: false, adCreativeTransition: false } }, gn = (function() {
            function e3(e4, t4, r2, n2) {
              this.listener = e4, this.adjustments = r2, this.video = void 0, this.drmManager = void 0, this.codecs = void 0, this.sink = void 0, this.observer = void 0, this.playerConfig = void 0, this.remoteDevicesListener = void 0, this.video = n2, this.listener = e4, this.drmManager = new Er({ video: this.video, listener: e4 }), this.codecs = /* @__PURE__ */ Object.create(null), this.sink = new Vr(this.listener, this.video), t4 && (this.remoteDevicesListener = Ur.lookForRemotePlaybackDevices(this.listener)), this.observer = new Br(this.video, e4);
            }
            var t3 = e3.prototype;
            return t3.videoElement = function() {
              return this.video;
            }, t3.delete = function() {
              var e4;
              this.reset(), null == (e4 = this.remoteDevicesListener) || e4.then(function(e5) {
                e5 && Ur.stopLookingForRemotePlaybackDevices(e5);
              });
            }, t3.configure = function(e4) {
              var t4 = e4.mode, r2 = e4.codec, n2 = e4.trackID;
              this.sink instanceof Vr && this.updateSinkByMode(t4), r2 ? this.codecs[n2] = r2 : e4.codec = this.codecs[n2], this.sink.configure(e4), this.drmManager.configure(e4), this.observer.onConfigure();
            }, t3.applyRPC = function(e4) {
              this.observer.trackRPC(e4), this.sink.invoke(e4);
            }, t3.getCurrentSink = function() {
              return this.sink;
            }, t3.reset = function() {
              this.sink.delete(), this.drmManager.reset(), this.sink = new Vr(this.listener, this.video), this.listener.onSinkTimeUpdate(), this.listener.onSinkBufferUpdate();
            }, t3.isProtected = function() {
              return this.drmManager.isProtected();
            }, t3.captureGesture = function() {
              this.video.played.length || this.sink.captureGesture();
            }, t3.destroy = function() {
              this.observer.unsubscribe(), this.delete();
            }, t3.isLowLatencyCapable = function() {
              return this.sink instanceof Xr;
            }, t3.onSegmentDiscontinuity = function() {
              this.sink.onSegmentDiscontinuity();
            }, t3.onPlayerConfigurationChanged = function(e4) {
              this.playerConfig = e4, this.sink.onPlayerConfigurationChanged(e4);
            }, t3.updateSinkByMode = function(e4) {
              switch (this.sink.delete(), e4) {
                case "chromecast":
                  this.sink = new Ur(this.listener);
                  break;
                case "mse-worker":
                  this.sink = new qr(this.listener, this.video);
                  break;
                case "passthrough":
                  this.sink = new cn(this.listener, this.video);
                  break;
                case "webview":
                  this.sink = new pn(this.listener, this.video, this.adjustments);
                  break;
                default:
                  this.sink = new Xr(this.listener, this.video);
              }
              this.playerConfig && this.sink.onPlayerConfigurationChanged(this.playerConfig);
            }, e3;
          })(), mn = 1111, yn = [{ CLASS: "timestamp", attributes: /* @__PURE__ */ new Set(["X-SERVER-TIME"]) }, { CLASS: "twitch-session", attributes: /* @__PURE__ */ new Set(["X-TV-TWITCH-SESSIONID"]) }, { CLASS: "twitch-stitched-ad", attributes: /* @__PURE__ */ new Set(["X-TV-TWITCH-AD-URL", "X-TV-TWITCH-AD-LINE-ITEM-ID", "X-TV-TWITCH-AD-AD-FORMAT", "X-TV-TWITCH-AD-DSA-VERSION", "X-TV-TWITCH-AD-CLICK-BEACON-ID", "X-TV-TWITCH-AD-POD-LENGTH", "X-TV-TWITCH-AD-POD-POSITION", "X-TV-TWITCH-AD-ROLL-TYPE", "X-TV-TWITCH-AD-RADS-TOKEN", "X-TV-TWITCH-AD-AD-SESSION-ID", "X-TV-TWITCH-AD-ADVERTISER-ID", "X-TV-TWITCH-AD-CREATIVE-ID", "X-TV-TWITCH-AD-ORDER-ID", "X-TV-TWITCH-AD-CLICK-TRACKING-URL", "X-TV-TWITCH-AD-DSA-SS-CONTEXT", "X-TV-TWITCH-AD-DSA-SS-LOCATION", "X-TV-TWITCH-AD-POD-FILLED-DURATION", "X-TV-TWITCH-AD-AF-ICR-AD-ID", "X-TV-TWITCH-AD-AF-ICR-CREATIVE-ID", "X-TV-TWITCH-AD-AF-ICR-MEDIA-DURATION", "X-TV-TWITCH-AD-LOUDNESS"]) }, { CLASS: "twitch-stream-source", attributes: /* @__PURE__ */ new Set(["X-TV-TWITCH-STREAM-SOURCE"]) }, { CLASS: "twitch-trigger", attributes: /* @__PURE__ */ new Set(["X-TV-TWITCH-TRIGGER-URL"]) }, { CLASS: "twitch-ad-quartile", attributes: /* @__PURE__ */ new Set(["X-TV-TWITCH-AD-QUARTILE"]) }], bn = function() {
            return { autoQualityMode: true, averageBitrate: 0, bandwidthEstimate: 0, bufferedPosition: 0, catchUpMode: "", channelMetadata: [], duration: 0, initialBufferDuration: 0, liveLatency: 0, liveLowLatency: false, liveLowLatencyEnabled: true, looping: false, path: "", position: 0, protocol: "", qualities: [], quality: { name: "", group: "", codecs: "", bitrate: 0, width: 0, height: 0, framerate: 0, isDefault: false, variantSource: "", variantId: "", sourceGroups: [] }, sessionData: {}, sessionId: void 0, sourceGroup: void 0, sourceGroups: [], startOffset: 0, state: c.IDLE, statistics: { bitrate: 0, framerate: 0, droppedFrames: 0, decodedFrames: 0, renderedFrames: 0 }, syncTime: 0, textTrack: null, textTracks: [], trackBufferedRanges: { audio: [], video: [] }, unavailableQualities: [], volume: 1 };
          }, En = require_wrapNativeSuper(), Sn = (function(e3) {
            function t3(t4, r2, n2, i2) {
              var o2;
              return (o2 = e3.call(this, n2) || this).source = t4, o2.code = r2, o2.message = n2, o2.fatal = i2, o2;
            }
            return Lr()(t3, e3), t3;
          })(r.n(En)()(Error)), Tn = (function(e3) {
            return e3[e3.WebGPUNotAvailable = -1e3] = "WebGPUNotAvailable", e3[e3.WebGPUAdapterNotAvailable = -1001] = "WebGPUAdapterNotAvailable", e3[e3.WebGPUDeviceNotAvailable = -1002] = "WebGPUDeviceNotAvailable", e3[e3.WebGPUAdapterInfoUndefined = -1003] = "WebGPUAdapterInfoUndefined", e3[e3.ShaderCompilationFailed = -1100] = "ShaderCompilationFailed", e3[e3.PipelineCreationFailed = -1101] = "PipelineCreationFailed", e3[e3.SamplerCreationFailed = -1102] = "SamplerCreationFailed", e3[e3.StreamCreationFailed = -1103] = "StreamCreationFailed", e3[e3.ModelInitFailed = -1104] = "ModelInitFailed", e3[e3.OutOfMemory = -1200] = "OutOfMemory", e3[e3.InvalidBuffer = -1201] = "InvalidBuffer", e3[e3.InvalidBufferSize = -1202] = "InvalidBufferSize", e3[e3.BufferCopyFailed = -1203] = "BufferCopyFailed", e3[e3.DeviceLost = -1300] = "DeviceLost", e3[e3.DeviceLostTooManyRetries = -1301] = "DeviceLostTooManyRetries", e3[e3.CanvasCreationFailed = -1400] = "CanvasCreationFailed", e3[e3.InferenceFailed = -1500] = "InferenceFailed", e3[e3.FetchFailed = -1600] = "FetchFailed", e3[e3.Render = -1700] = "Render", e3[e3.Init = -1800] = "Init", e3[e3.FrameCaptureFailed = -1900] = "FrameCaptureFailed", e3[e3.Performance = -2e3] = "Performance", e3[e3.PerformanceLowRenderFramerate = -2001] = "PerformanceLowRenderFramerate", e3[e3.PerformanceLowCaptureFramerate = -2002] = "PerformanceLowCaptureFramerate", e3[e3.PerformanceTooManyOverbudgetFrames = -2010] = "PerformanceTooManyOverbudgetFrames", e3[e3.PerformanceTooManySkippedFrames = -2011] = "PerformanceTooManySkippedFrames", e3[e3.PerformanceTooManyDroppedSourceFrames = -2012] = "PerformanceTooManyDroppedSourceFrames", e3[e3.PerformanceTooManyMissedFrames = -2013] = "PerformanceTooManyMissedFrames", e3[e3.ResourceClosed = -9998] = "ResourceClosed", e3[e3.BadParameters = -9999] = "BadParameters", e3;
          })({}), _n = (function() {
            function e3() {
              this.frameStats = void 0, this.resetStats();
            }
            var t3 = e3.prototype;
            return t3.init = function() {
              var e4 = this;
              return new WritableStream({ write: function(t4) {
                t4 && Promise.resolve().then(function() {
                  e4.onFrameRecord(t4);
                }).catch(function(e5) {
                  Be.error(e5);
                });
              } });
            }, t3.stats = function() {
              return f()({}, this.frameStats);
            }, t3.resetStats = function() {
              this.frameStats = { received: 0, overBudget: 0, skipped: 0, failed: 0, transformed: 0, rendered: 0, transformTime: 0, renderTime: 0, receiveDelay: 0, endToEnd: 0 };
            }, t3.onFrameRecord = function(e4) {
              var t4 = { receive: e4.timings.received - e4.timings.captured, transform: e4.timings.transformEnd - e4.timings.transformStart, render: e4.timings.renderEnd - e4.timings.renderStart, endToEnd: e4.timings.renderEnd - e4.timings.captured };
              this.frameStats.received++, this.frameStats.receiveDelay += t4.receive, e4.skipped && (this.frameStats.skipped++, Be.warn("[FrameRecordSink]: Frame " + e4.number + " was skipped. Frame receive delay: " + t4.receive + ", Initial budget: " + e4.budget + "ms, Min budget: " + e4.minBudget + "ms", t4, e4)), e4.transformed && (this.frameStats.transformed++, this.frameStats.transformTime += t4.transform), e4.rendered && (this.frameStats.rendered++, this.frameStats.renderTime += t4.render), e4.failed && (this.frameStats.failed++, Be.warn("[FrameRecordSink]: Frame " + e4.number + " failed to to transform or render. See error log for details.", t4, e4)), t4.endToEnd >= e4.budget && (this.frameStats.overBudget++, Be.warn("[FrameRecordSink]: Frame " + e4.number + " was overbudget. End to end time: " + t4.endToEnd + "ms Budget: " + e4.budget + "ms", t4, e4)), this.frameStats.endToEnd += t4.endToEnd;
            }, e3;
          })(), Cn = (function() {
            function e3(e4, t4, r2, n2) {
              this.inputFrameStream = e4, this.sink = t4, this.listeners = r2, this.config = n2, this.pipeline = void 0, this.transformer = void 0, this.frameRecordSink = void 0, this.frameRecordSink = new _n();
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      this.transformer = t4, e6.prev = 1, r2 = this.frameRecordSink.init(), this.pipeline = this.createPipeline(), this.pipeline.readable.pipeTo(r2).catch(function(e7) {
                        i2.listeners.error(new Sn("VideoFramePipeline", Tn.PipelineCreationFailed, "Failed to pipe to frameRecordStream: " + (null == e7 ? void 0 : e7.message), true));
                      }), this.inputFrameStream.pipeThrough(this.pipeline), e6.next = 3;
                      break;
                    case 2:
                      return e6.prev = 2, n2 = e6.catch(1), e6.abrupt("return", new Sn("VideoFramePipeline", Tn.PipelineCreationFailed, "Failed to init pipeline: " + (null == n2 ? void 0 : n2.message), true));
                    case 3:
                      return e6.abrupt("return", void 0);
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[1, 2]]);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.stats = function() {
              return this.frameRecordSink.stats();
            }, t3.resetStats = function() {
              this.frameRecordSink.resetStats();
            }, t3.createPipeline = function() {
              var e4, t4 = this, r2 = new TransformStream({ transform: (e4 = Ne()(Ue().mark(function e5(r3, n2) {
                var i2, o2, a2, s2, u2, c2, l2, d2, f2, h2, p2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (i2 = r3.frame, o2 = r3.record, a2 = performance.now(), s2 = void 0, u2 = [], c2 = Math.min(a2 - o2.timings.captured, 0), o2.timings.received = a2, l2 = 1e3 / t4.config.minimumFramerate - (t4.config.includeReceiveDelay ? c2 : 0), o2.budget = l2, o2.minBudget = 0, d2 = (function() {
                        var e7 = Ne()(Ue().mark(function e8() {
                          var r4, a3;
                          return Ue().wrap(function(e9) {
                            for (; ; ) switch (e9.prev = e9.next) {
                              case 0:
                                return e9.next = 1, t4.render(i2, o2, s2);
                              case 1:
                                r4 = e9.sent, (a3 = r4.renderError) && u2.push(a3), i2.close(), o2.failed = !!u2.length, n2.enqueue(o2), u2.forEach(function(e10) {
                                  return t4.handleErrorInternal(e10);
                                });
                              case 2:
                              case "end":
                                return e9.stop();
                            }
                          }, e8);
                        }));
                        return function() {
                          return e7.apply(this, arguments);
                        };
                      })(), !(l2 <= 0)) {
                        e6.next = 2;
                        break;
                      }
                      return o2.skipped = true, e6.next = 1, d2();
                    case 1:
                      return e6.abrupt("return");
                    case 2:
                      return e6.next = 3, t4.frameToTexture(i2, o2, l2);
                    case 3:
                      return f2 = e6.sent, h2 = f2.transformError, p2 = f2.texture, s2 = p2, h2 && u2.push(h2), e6.next = 4, d2();
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              })), function(t5, r3) {
                return e4.apply(this, arguments);
              }) });
              return r2;
            }, t3.frameToTexture = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                var i2, o2, a2, s2, u2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return i2 = performance.now(), o2 = void 0, r2.timings.transformStart = i2, e6.next = 1, this.transformer.transform(t4, r2, n2);
                    case 1:
                      return a2 = e6.sent, s2 = a2.texture, (u2 = a2.error) ? (Be.warn("[VideoFramePipeline]: Failed to transform frame " + r2.number), o2 = u2) : s2 && (r2.transformed = true), r2.timings.transformEnd = performance.now(), e6.abrupt("return", { transformError: o2, texture: s2 });
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.render = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                var i2, o2, a2, s2, u2, c2, l2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return i2 = performance.now(), r2.timings.renderStart = i2, o2 = void 0, e6.prev = 1, u2 = null != (a2 = null == n2 ? void 0 : n2.width) ? a2 : t4.displayWidth, c2 = null != (s2 = null == n2 ? void 0 : n2.height) ? s2 : t4.displayHeight, this.sink.setDimensions(u2, c2), e6.next = 2, this.sink.render(null != n2 ? n2 : t4);
                    case 2:
                      (o2 = e6.sent) || (r2.rendered = true), e6.next = 4;
                      break;
                    case 3:
                      e6.prev = 3, l2 = e6.catch(1), Be.warn("[VideoFramePipeline]: Failed to render frame " + r2.number), o2 = l2;
                    case 4:
                      return r2.timings.renderEnd = performance.now(), e6.abrupt("return", { renderError: o2 });
                    case 5:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[1, 3]]);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.handleErrorInternal = function(e4) {
              Be.warn("[VideoFramePipeline]: Error transforming frame", e4), this.listeners.error(e4);
            }, e3;
          })(), kn = function() {
            return { pipelineId: "", number: 0, budget: 0, minBudget: 0, blocked: false, skipped: false, transformed: false, rendered: false, overbudget: false, failed: false, timings: { composited: 0, captured: 0, received: 0, transformStart: 0, transformEnd: 0, renderStart: 0, renderEnd: 0 } };
          }, wn = (function() {
            function e3(e4, t4) {
              this.config = e4, this.onError = t4, this.active = false, this.frameStream = void 0, this.frameWriter = void 0, this.stopCapture = void 0, this.captureStats = void 0, this.frameMetadata = { pipelineId: "", match: { width: -1, height: -1 } }, this.resetStats();
            }
            var t3 = e3.prototype;
            return t3.init = function() {
              try {
                this.frameStream = new TransformStream({}, { highWaterMark: this.config.maxQueueSize }), this.frameWriter = this.frameStream.writable.getWriter();
              } catch (e4) {
                return new Sn("VideoFrameCaptureStream", Tn.StreamCreationFailed, e4.message, true);
              }
            }, t3.start = function(e4) {
              this.active ? Be.warn("[VideoFrameCaptureStream]: Start called, but already capturing") : (this.startRVFC(e4), this.active = true);
            }, t3.stop = function() {
              var e4;
              this.active = false, null == (e4 = this.stopCapture) || e4.call(this);
            }, t3.setVideoFrameMetadata = function(e4) {
              this.frameMetadata = e4;
            }, t3.stats = function() {
              return this.captureStats;
            }, t3.resetStats = function() {
              this.captureStats = { captured: 0, missed: 0, blocked: 0, rVFCDelay: 0 };
            }, t3.getStream = function() {
              return this.frameStream.readable;
            }, t3.startRVFC = function(e4) {
              var t4, r2 = this, n2 = void 0, i2 = -1;
              null == (t4 = this.stopCapture) || t4.call(this), this.stopCapture = function() {
                return e4.cancelVideoFrameCallback(i2);
              };
              var o2 = (function() {
                var t5 = Ne()(Ue().mark(function t6(a2, s2) {
                  var u2, c2, l2, d2, f2;
                  return Ue().wrap(function(t7) {
                    for (; ; ) switch (t7.prev = t7.next) {
                      case 0:
                        if (r2.active) {
                          t7.next = 1;
                          break;
                        }
                        return t7.abrupt("return");
                      case 1:
                        if (t7.prev = 1, c2 = performance.now(), r2.captureStats.rVFCDelay += c2 - a2, l2 = new VideoFrame(e4), (d2 = kn()).number = s2.presentedFrames, d2.timings.composited = a2, d2.timings.captured = c2, l2.displayWidth === r2.frameMetadata.match.width && l2.displayHeight === r2.frameMetadata.match.height && (d2.pipelineId = r2.frameMetadata.pipelineId), !((null != (u2 = r2.frameWriter.desiredSize) ? u2 : 0) < 1)) {
                          t7.next = 3;
                          break;
                        }
                        return r2.captureStats.blocked++, d2.blocked = true, t7.next = 2, r2.frameWriter.write({ frame: l2, record: d2 });
                      case 2:
                        t7.next = 4;
                        break;
                      case 3:
                        r2.frameWriter.write({ frame: l2, record: d2 }).catch(function(e5) {
                          r2.onError(new Sn("CaptureStream", Tn.FrameCaptureFailed, "Failed to write frame to stream: " + ((null == e5 ? void 0 : e5.message) || "unknown error"), true));
                        });
                      case 4:
                        void 0 === n2 ? n2 = s2.presentedFrames : s2.presentedFrames - n2 > 1 && (r2.captureStats.missed += s2.presentedFrames - n2), n2 = s2.presentedFrames, r2.captureStats.captured++, i2 = e4.requestVideoFrameCallback(o2), t7.next = 6;
                        break;
                      case 5:
                        t7.prev = 5, f2 = t7.catch(1), r2.errorHandler(new Sn("CaptureStream", Tn.FrameCaptureFailed, "Failed to write frame to stream: " + ((null == f2 ? void 0 : f2.message) || "unknown error"), true));
                      case 6:
                      case "end":
                        return t7.stop();
                    }
                  }, t6, null, [[1, 5]]);
                }));
                return function(e5, r3) {
                  return t5.apply(this, arguments);
                };
              })();
              Be.log("[VideoFrameCaptureStream]: Started", e4), i2 = e4.requestVideoFrameCallback(o2);
            }, t3.errorHandler = function(e4) {
              this.stop(), this.onError(e4);
            }, e3;
          })(), Pn = (function() {
            function e3(e4, t4) {
              this.windowContext = e4, this.config = t4, this.adapter = void 0, this.adapterInfo = void 0, this.device = void 0, this.queue = void 0, this.timingInfoEnabled = void 0, this.storageFormat = void 0;
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4, r2, n2, i2, o2, a2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.windowContext.navigator.gpu) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", new Sn("WebGPUContext", Tn.WebGPUNotAvailable, "WebGPU not available", true));
                    case 1:
                      return e6.next = 2, this.windowContext.navigator.gpu.requestAdapter({});
                    case 2:
                      if (t4 = e6.sent) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", new Sn("WebGPUContext", Tn.WebGPUAdapterNotAvailable, "WebGPU Adapter not available", true));
                    case 3:
                      if (t4.info) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", new Sn("WebGPUContext", Tn.WebGPUAdapterInfoUndefined, "WebGPU adapter.info is undefined", true));
                    case 4:
                      return r2 = [].concat(this.config.requiredFeatures), n2 = {}, i2 = "bgra8unorm" === this.windowContext.navigator.gpu.getPreferredCanvasFormat() && t4.features.has("bgra8unorm-storage"), o2 = this.config.timingInfoEnabled && t4.features.has("timestamp-query"), i2 && r2.push("bgra8unorm-storage"), o2 && r2.push("timestamp-query"), n2.requiredFeatures = r2, e6.next = 5, this.tryGetDevice(t4, n2);
                    case 5:
                      if (a2 = e6.sent) {
                        e6.next = 6;
                        break;
                      }
                      return e6.abrupt("return", new Sn("WebGPUContext", Tn.WebGPUDeviceNotAvailable, "WebGPU Device not available", true));
                    case 6:
                      this.adapterInfo = t4.info, this.timingInfoEnabled = o2, this.storageFormat = i2 ? "bgra8unorm" : "rgba8unorm", this.adapter = t4, this.device = a2, this.queue = a2.queue, Be.info("[WebGPUContext]: WebGPU context initialized", this.properties());
                    case 7:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.properties = function() {
              var e4;
              if (!this.adapterInfo) return { architecture: "", description: "", device: "", vendor: "", adapterFeatures: "", deviceFeatures: "", wgslFeatures: "" };
              var t4 = "";
              this.adapter.features.forEach(function(e5) {
                t4 += e5 + ",";
              });
              var r2 = "";
              this.device.features.forEach(function(e5) {
                r2 += e5 + ",";
              });
              var n2 = "";
              return null == (e4 = navigator.gpu.wgslLanguageFeatures) || e4.forEach(function(e5) {
                n2 += e5 + ",";
              }), { architecture: this.adapterInfo.architecture, description: this.adapterInfo.description, device: this.adapterInfo.device, vendor: this.adapterInfo.vendor, adapterFeatures: t4, deviceFeatures: r2, wgslFeatures: n2 };
            }, t3.createBuffer = function(e4, t4) {
              return this.device.pushErrorScope("out-of-memory"), this.device.createBuffer({ size: e4, usage: t4 });
            }, t3.setBufferData = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (void 0 === n2 && (n2 = true), this.device.queue.writeBuffer(t4, 0, r2), !n2) {
                        e6.next = 1;
                        break;
                      }
                      return e6.next = 1, this.device.queue.onSubmittedWorkDone();
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.createShaderModule = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return r2 = this.device.createShaderModule({ code: t4 }), e6.next = 1, r2.getCompilationInfo();
                    case 1:
                      if (!((n2 = e6.sent).messages.length > 0)) {
                        e6.next = 2;
                        break;
                      }
                      return Be.error("[WebGPUContext]: Error compiling shader", n2.messages), e6.abrupt("return", { err: new Sn("WebGPUContext", Tn.ShaderCompilationFailed, "Compilation failed: " + n2.messages.join(), true) });
                    case 2:
                      return e6.abrupt("return", { module: r2 });
                    case 3:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.tryGetDevice = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.prev = 0, e6.next = 1, t4.requestDevice(r2);
                    case 1:
                    case 4:
                      return e6.abrupt("return", e6.sent);
                    case 2:
                      return e6.prev = 2, e6.catch(0), e6.prev = 3, e6.next = 4, t4.requestDevice(null);
                    case 5:
                      return e6.prev = 5, e6.catch(3), e6.abrupt("return", void 0);
                    case 6:
                    case "end":
                      return e6.stop();
                  }
                }, e5, null, [[0, 2], [3, 5]]);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), e3;
          })(), An = (function() {
            function e3(e4, t4, r2) {
              this.canvasEl = e4, this.gpuContext = t4, this.onError = r2, this.canvas = void 0, this.device = void 0, this.canvasContext = void 0, this.pipelines = void 0, this.device = t4.device, this.pipelines = /* @__PURE__ */ new Map();
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.prev = 0, e6.next = 1, this.setCanvas(this.canvasEl.transferControlToOffscreen());
                    case 1:
                      e6.next = 3;
                      break;
                    case 2:
                      return e6.prev = 2, t4 = e6.catch(0), e6.abrupt("return", new Sn("CanvasSink", Tn.CanvasCreationFailed, t4.message, true));
                    case 3:
                      return e6.abrupt("return", void 0);
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[0, 2]]);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.render = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2, a2, s2, u2, c2, l2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.canvasContext && this.canvas) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", new Sn("CanvasSink", Tn.Render, "Failed to get WebGPU Canvas context", true));
                    case 1:
                      return r2 = t4 instanceof VideoFrame, e6.next = 2, this.getPipeline(this.canvas.width, this.canvas.height, r2);
                    case 2:
                      if (n2 = e6.sent, i2 = n2.pipeline, !(o2 = n2.error) && i2) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", o2);
                    case 3:
                      a2 = r2 ? this.device.importExternalTexture({ source: t4 }) : t4.createView(), s2 = i2.getBindGroupLayout(0), u2 = this.device.createBindGroup({ layout: s2, entries: [{ binding: 0, resource: this.canvasContext.getCurrentTexture().createView() }, { binding: 1, resource: a2 }] }), this.device.pushErrorScope("validation"), c2 = this.device.createCommandEncoder(), (l2 = c2.beginComputePass()).setPipeline(i2), l2.setBindGroup(0, u2), l2.dispatchWorkgroups(Math.ceil(this.canvas.width / 16), Math.ceil(this.canvas.height / 16), 1), l2.end(), this.device.queue.submit([c2.finish()]), this.checkForErrorsAsync();
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.setDimensions = function(e4, t4) {
              !this.canvas || e4 === this.canvas.width && t4 === this.canvas.height || (this.canvas.width = e4, this.canvas.height = t4);
            }, t3.setCanvas = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (this.canvas = t4, this.canvasContext = t4.getContext("webgpu"), this.canvasContext) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", new Sn("CanvasSink", Tn.Render, "Failed to get WebGPU Canvas context", true));
                    case 1:
                      return r2 = this.gpuContext.storageFormat, n2 = { device: this.device, format: r2, usage: GPUTextureUsage.STORAGE_BINDING, viewFormats: [r2], alphaMode: "opaque" }, this.device.pushErrorScope("validation"), this.canvasContext.configure(n2), e6.next = 2, this.device.popErrorScope();
                    case 2:
                      if (!e6.sent) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", new Sn("CanvasSink", Tn.Render, "Failed to configure Canvas context", true));
                    case 3:
                      return e6.abrupt("return", void 0);
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.getPipeline = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                var i2, o2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (i2 = n2 + "_" + t4 + "_" + r2, !this.pipelines.has(i2) || !this.pipelines.get(i2)) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", { pipeline: this.pipelines.get(i2), error: void 0 });
                    case 1:
                      return e6.abrupt("return", this.makePipeline(t4, r2, n2).then(function(e7) {
                        return e7.pipeline && o2.pipelines.set(i2, e7.pipeline), e7;
                      }));
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.makePipeline = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                var i2, o2, a2, s2, u2, c2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return i2 = this.getShader(t4, r2, n2), e6.next = 1, this.createShaderModule(i2);
                    case 1:
                      if (o2 = e6.sent, a2 = o2.module, s2 = o2.error, a2 && !s2) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", { error: s2 });
                    case 2:
                      return this.device.pushErrorScope("validation"), u2 = this.device.createComputePipeline({ layout: "auto", compute: { module: a2, entryPoint: "main" }, label: "canvas-sink_" + t4 + "_" + r2 + "_" + n2 }), e6.next = 3, this.device.popErrorScope();
                    case 3:
                      if (!(c2 = e6.sent)) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", { error: new Sn("CanvasSink", Tn.PipelineCreationFailed, "" + c2.message, true) });
                    case 4:
                      return e6.abrupt("return", { pipeline: u2 });
                    case 5:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.createShaderModule = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return r2 = this.device.createShaderModule({ code: t4 }), e6.next = 1, r2.getCompilationInfo();
                    case 1:
                      if (!((n2 = e6.sent).messages.length > 0)) {
                        e6.next = 2;
                        break;
                      }
                      return Be.error("[CanvasSink]: Error compiling shader", n2.messages), e6.abrupt("return", { error: new Sn("CanvasSink", Tn.ShaderCompilationFailed, "Shader compilation failed: " + n2.messages.join(), true) });
                    case 2:
                      return e6.abrupt("return", { module: r2 });
                    case 3:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.getShader = function(e4, t4, r2) {
              return "\n        @group(0) @binding(0) var outputTexture: texture_storage_2d<" + this.gpuContext.storageFormat + ", write>;\n        @group(0) @binding(1) var inputTexture: " + (r2 ? "texture_external" : "texture_2d<f32>") + ";\n\n        @compute @workgroup_size(16,16,1)\n        fn main(@builtin(global_invocation_id) global_id : vec3<u32>) {\n            if (global_id.x >= " + e4 + " || global_id.y >= " + t4 + ") {\n                return;\n            }\n            let color = " + (r2 ? "textureLoad(inputTexture, global_id.xy);" : "textureLoad(inputTexture, global_id.xy, 0);") + ";\n            textureStore(outputTexture, global_id.xy, color);\n        }\n        ";
            }, t3.checkForErrorsAsync = function() {
              var e4 = this;
              this.device.popErrorScope().then(function(t4) {
                t4 && (Be.warn("[CanvasSink]: WebGPU error encountered", t4), e4.onErrorInternal(new Sn("CanvasSink", Tn.Render, "Failed to render: " + t4.message, true)));
              }).catch(function(t4) {
                e4.onError(new Sn("CanvasSink", Tn.BadParameters, "Failed to popErrorScope: " + ((null == t4 ? void 0 : t4.message) || "unknown error"), true));
              });
            }, t3.onErrorInternal = function(e4) {
              this.onError(e4);
            }, e3;
          })(), In = (function() {
            function e3(e4, t4) {
              this.name = e4, this.listeners = t4, this.frameBudgetTimeout = -1;
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", Promise.resolve(void 0));
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.transform = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2) {
                var i2, o2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.next = 1, Promise.race([this.doTransform(t4, r2), this.frameBudgetExceededPassthrough(n2)]).catch(function(e7) {
                        return clearTimeout(o2.frameBudgetTimeout), { frame: t4, error: e7 };
                      });
                    case 1:
                      return i2 = e6.sent, clearTimeout(this.frameBudgetTimeout), e6.abrupt("return", i2);
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2) {
                return e4.apply(this, arguments);
              };
            })(), t3.doTransform = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.next = 1, this.transformFn(t4, r2);
                    case 1:
                      return n2 = e6.sent, e6.abrupt("return", { texture: n2 });
                    case 2:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.frameBudgetExceededPassthrough = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", new Promise(function(e7) {
                        return r2.frameBudgetTimeout = window.setTimeout(function() {
                          e7({});
                        }, t4);
                      }));
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.transformFn = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", void 0);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), e3;
          })(), Dn = (function(e3) {
            function t3() {
              return e3.call(this, "PassthroughTransformer", { error: function() {
              } }) || this;
            }
            return Lr()(t3, e3), t3.prototype.transform = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", {});
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3;
          })(In);
          function xn(e3, t3) {
            (null == t3 || t3 > e3.length) && (t3 = e3.length);
            for (var r2 = 0, n2 = Array(t3); r2 < t3; r2++) n2[r2] = e3[r2];
            return n2;
          }
          var Mn = (function() {
            function e3(e4, t4, r2) {
              this.config = e4, this.getStats = t4, this.listeners = r2, this.monitorInterval = -1, this.active = false, this.validators = [], Be.log("[PerformanceMontior]: Config", this.config), this.validators.push(/* @__PURE__ */ (function(e5) {
                return { validate: function(t5) {
                  var r3 = 1 - t5.source_presented / t5.source;
                  if (r3 >= e5.maxDroppedFramesPct) return { code: Tn.PerformanceTooManyDroppedSourceFrames, fatal: true, reason: "Too many dropped source frames. Dropped: " + (100 * r3).toFixed(0) + "%, Max: " + 100 * e5.maxDroppedFramesPct + "% Presented frames: " + t5.source_presented + ", Total frames: " + t5.source };
                } };
              })({ maxDroppedFramesPct: e4.frameRateDiffPercentage }), /* @__PURE__ */ (function(e5) {
                return { validate: function(t5) {
                  var r3 = t5.captured, n2 = t5.source_presented, i2 = n2 - n2 * e5.frameCountDiffPercentage;
                  if (r3 < i2) return { code: Tn.PerformanceLowRenderFramerate, fatal: true, reason: "Capture framerate is too low. Source Presented: " + n2 + " Min: " + i2 + " Captured: " + r3 + " Config: " + 100 * e5.frameCountDiffPercentage + "%" };
                } };
              })({ frameCountDiffPercentage: this.config.frameCountDiffPercentage, captureRateDiffPercentage: this.config.frameRateDiffPercentage }), /* @__PURE__ */ (function(e5) {
                return { validate: function(t5) {
                  var r3 = t5.source, n2 = t5.rendered, i2 = t5.received;
                  if (t5.overBudget > i2 * e5.frameCountDiffPercentage) return { code: Tn.PerformanceTooManyOverbudgetFrames, fatal: true, reason: "Too many overbudget frames. Received: " + i2 + " Max: " + i2 * e5.frameCountDiffPercentage + " Have: " + t5.overBudget + " Config: " + 100 * e5.frameCountDiffPercentage + "%" };
                  if (t5.skipped > i2 * e5.frameCountDiffPercentage) return { code: Tn.PerformanceTooManySkippedFrames, fatal: true, reason: "Too many skipped frames. Received: " + i2 + " Max: " + i2 * e5.frameCountDiffPercentage + " Have: " + t5.skipped + " Config: " + 100 * e5.frameCountDiffPercentage + "%" };
                  var o2 = r3 - r3 * e5.renderFramerateDiffPercentage;
                  return n2 < o2 ? { code: Tn.PerformanceLowRenderFramerate, fatal: true, reason: "Render framerate is too low. Source: " + r3 + " Min: " + o2 + " Rendered: " + n2 + " Config: " + 100 * e5.renderFramerateDiffPercentage + "%" } : void 0;
                } };
              })({ frameCountDiffPercentage: this.config.frameCountDiffPercentage, renderFramerateDiffPercentage: this.config.frameRateDiffPercentage }));
            }
            var t3 = e3.prototype;
            return t3.start = function() {
              var e4 = this;
              if (this.config.enable) if (this.monitoring()) Be.log("[PerformanceMonitor]: Resume called on active instance");
              else {
                Be.log("[PerformanceMonitor]: Monitoring started");
                var t4 = this.getStats();
                window.clearInterval(this.monitorInterval), this.monitorInterval = window.setInterval(function() {
                  var r2 = e4.getStats(), n2 = e4.validatePerformance(t4, r2);
                  n2 && e4.listeners.performanceBreach(n2), t4 = r2;
                }, this.config.monitorIntervalMs);
              }
            }, t3.stop = function() {
              window.clearInterval(this.monitorInterval), this.monitorInterval = -1;
            }, t3.monitoring = function() {
              return this.monitorInterval > 0;
            }, t3.validatePerformance = function(e4, t4) {
              var r2 = this.diffFrameStats(e4.frames, t4.frames);
              Be.log("[PerformanceMonitor] Stats:", e4, t4), Be.log("[PerformanceMonitor] Stats diff:", r2);
              var n2 = this.config.monitorIntervalMs / 1e3 * 20;
              if (r2.source <= n2) Be.log("[PerformanceMonitor]: Not enough source frames to validate. Have: " + r2.source + " Need: " + n2);
              else for (var i2, o2 = (function(e5, t5) {
                var r3 = "undefined" != typeof Symbol && e5[Symbol.iterator] || e5["@@iterator"];
                if (r3) return (r3 = r3.call(e5)).next.bind(r3);
                if (Array.isArray(e5) || (r3 = (function(e6, t6) {
                  if (e6) {
                    if ("string" == typeof e6) return xn(e6, t6);
                    var r4 = {}.toString.call(e6).slice(8, -1);
                    return "Object" === r4 && e6.constructor && (r4 = e6.constructor.name), "Map" === r4 || "Set" === r4 ? Array.from(e6) : "Arguments" === r4 || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(r4) ? xn(e6, t6) : void 0;
                  }
                })(e5)) || t5 && e5 && "number" == typeof e5.length) {
                  r3 && (e5 = r3);
                  var n3 = 0;
                  return function() {
                    return n3 >= e5.length ? { done: true } : { done: false, value: e5[n3++] };
                  };
                }
                throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
              })(this.validators); !(i2 = o2()).done; ) {
                var a2 = i2.value.validate(r2);
                if (a2) return a2;
              }
            }, t3.diffFrameStats = function(e4, t4) {
              for (var r2 = { source: 0, source_presented: 0, captured: 0, missed: 0, blocked: 0, received: 0, skipped: 0, transformed: 0, rendered: 0, failed: 0, overBudget: 0 }, n2 = 0, i2 = Object.entries(t4); n2 < i2.length; n2++) {
                var o2 = i2[n2], a2 = o2[0], s2 = o2[1];
                r2[a2] = s2 - e4[a2];
              }
              return r2;
            }, e3;
          })(), Rn = (function(e3) {
            return e3[e3.External = 0] = "External", e3[e3.Texture = 1] = "Texture", e3[e3.Buffer = 2] = "Buffer", e3;
          })({});
          function Ln(e3, t3, r2) {
            return Array.from({ length: e3 }, function(e4, t4) {
              return t4;
            }).map(r2).join(t3);
          }
          var On = (function(e3) {
            return e3[e3.Default = 0] = "Default", e3[e3.HostToDevice = 1] = "HostToDevice", e3[e3.DeviceToHost = 4] = "DeviceToHost", e3[e3.TimingQuery = 8] = "TimingQuery", e3;
          })({}), Nn = (function() {
            function e3(e4, t4, r2) {
              void 0 === r2 && (r2 = On.Default), this.bytesize = t4, this.usage = r2, this.deviceBuffer = void 0, this.stagingBuffer = void 0, this.closed = false;
              var n2 = GPUBufferUsage.STORAGE;
              r2 === On.HostToDevice ? n2 |= GPUBufferUsage.COPY_DST : r2 === On.DeviceToHost ? (n2 |= GPUBufferUsage.COPY_SRC, this.stagingBuffer = e4.createBuffer(t4, GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ)) : r2 === On.TimingQuery && (n2 |= GPUBufferUsage.QUERY_RESOLVE | GPUBufferUsage.COPY_DST | GPUBufferUsage.COPY_SRC, this.stagingBuffer = e4.createBuffer(t4, GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ)), this.deviceBuffer = e4.createBuffer(t4, n2);
            }
            var t3 = e3.prototype;
            return t3.copyToDevice = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.closed) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", new Sn("Buffer", Tn.ResourceClosed, "Buffer has been closed", true));
                    case 1:
                      return e6.prev = 1, e6.next = 2, t4.setBufferData(this.deviceBuffer, r2, false);
                    case 2:
                      e6.next = 4;
                      break;
                    case 3:
                      return e6.prev = 3, n2 = e6.catch(1), e6.abrupt("return", new Sn("Buffer", Tn.BadParameters, "Failed to copy to host: " + ((null == n2 ? void 0 : n2.message) || "unknown"), true));
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[1, 3]]);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.copyToHost = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.closed) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", { err: new Sn("Buffer", Tn.ResourceClosed, "Buffer has been closed", true) });
                    case 1:
                      if (this.stagingBuffer && this.usage !== On.DeviceToHost) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", { err: new Sn("Buffer", Tn.BadParameters, "Buffer usage is " + this.usage + ", but should be BufferUsage.DeviceToHost", true) });
                    case 2:
                      return e6.prev = 2, (r2 = t4.createCommandEncoder()).copyBufferToBuffer(this.deviceBuffer, 0, this.stagingBuffer, 0, this.bytesize), t4.queue.submit([r2.finish()]), e6.next = 3, t4.queue.onSubmittedWorkDone();
                    case 3:
                      return e6.next = 4, this.stagingBuffer.mapAsync(GPUMapMode.READ);
                    case 4:
                      return n2 = structuredClone(this.stagingBuffer.getMappedRange()), this.stagingBuffer.unmap(), e6.abrupt("return", { buffer: n2 });
                    case 5:
                      return e6.prev = 5, i2 = e6.catch(2), e6.abrupt("return", { err: new Sn("Buffer", Tn.BufferCopyFailed, "Failed to copy to host: " + ((null == i2 ? void 0 : i2.message) || "unknown"), true) });
                    case 6:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[2, 5]]);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.close = function() {
              var e4;
              this.closed = true, this.deviceBuffer.destroy(), null == (e4 = this.stagingBuffer) || e4.destroy();
            }, e3;
          })(), Fn = (function() {
            function e3(e4, t4, r2, n2, i2) {
              void 0 === i2 && (i2 = On.Default), this.usage = i2, this.stagingBuffer = void 0, this.deviceTexture = void 0, this.closed = false;
              var o2 = GPUTextureUsage.STORAGE_BINDING | GPUTextureUsage.TEXTURE_BINDING;
              i2 === On.HostToDevice ? o2 |= GPUTextureUsage.COPY_DST : i2 === On.DeviceToHost ? (Be.warn("[Buffer]: Copying texture to host buffer only supports 32bpp formats."), o2 |= GPUTextureUsage.COPY_SRC, this.stagingBuffer = e4.createBuffer(256 * Math.ceil(4 * t4 / 256) * r2, GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ)) : i2 === On.TimingQuery && Be.warn("[Buffer]: BufferUsage.TimingQuery is not valid for Texture"), this.deviceTexture = e4.device.createTexture({ size: { width: t4, height: r2 }, format: n2, usage: o2 });
            }
            var t3 = e3.prototype;
            return t3.copyToHost = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4) {
                var r2, n2, i2, o2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.closed) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", { err: new Sn("Texture", Tn.ResourceClosed, "Texture has been closed", true) });
                    case 1:
                      if (this.stagingBuffer && this.usage !== On.DeviceToHost) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", { err: new Sn("Texture", Tn.BadParameters, "Buffer usage is " + this.usage + ", but should be BufferUsage.DeviceToHost", true) });
                    case 2:
                      return Be.debug("[Buffer]: copying texture", this.deviceTexture), e6.prev = 3, (r2 = t4.createCommandEncoder()).copyTextureToBuffer({ texture: this.deviceTexture, mipLevel: 0, origin: [0, 0, 0], aspect: "all" }, { buffer: this.stagingBuffer, offset: 0, bytesPerRow: 256 * Math.ceil(4 * this.deviceTexture.width / 256), rowsPerImage: this.deviceTexture.height }, [this.deviceTexture.width, this.deviceTexture.height, 1]), t4.queue.submit([r2.finish()]), e6.next = 4, t4.queue.onSubmittedWorkDone();
                    case 4:
                      return e6.next = 5, this.stagingBuffer.mapAsync(GPUMapMode.READ);
                    case 5:
                      return n2 = this.stagingBuffer.getMappedRange(), i2 = structuredClone(n2), this.stagingBuffer.unmap(), e6.abrupt("return", { buffer: i2 });
                    case 6:
                      return e6.prev = 6, o2 = e6.catch(3), e6.abrupt("return", { err: new Sn("Texture", Tn.BufferCopyFailed, "Failed to copy to host: " + ((null == o2 ? void 0 : o2.message) || "unknown"), true) });
                    case 7:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[3, 6]]);
              }));
              return function(t4) {
                return e4.apply(this, arguments);
              };
            })(), t3.close = function() {
              var e4;
              this.closed = true, this.deviceTexture.destroy(), null == (e4 = this.stagingBuffer) || e4.destroy();
            }, e3;
          })(), Un = (function(e3) {
            return e3.F16 = "f16", e3.F32 = "f32", e3;
          })({}), Vn = (function() {
            function e3(e4, t4, r2, n2, i2, o2, a2) {
              void 0 === i2 && (i2 = 1), void 0 === o2 && (o2 = true), void 0 === a2 && (a2 = false), this.inFilters = e4, this.outFilters = t4, this.kernelSize = r2, this.scaleFactor = n2, this.outPadding = i2, this.convertToYuv = o2, this.hostReadableOutput = a2, this._configuration = void 0, this._activation = void 0, this._weights = void 0, this._biases = void 0, this._inputs = void 0, this._outputs = void 0, this._pipeline = void 0, this._outputShape = void 0, this._sampler = void 0;
            }
            var t3 = e3.prototype;
            return t3.configure = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2, c2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return t4.device.pushErrorScope("validation"), this._outputShape = [Math.ceil(r2.inputShape[0] * this.scaleFactor), Math.ceil(r2.inputShape[1] * this.scaleFactor), this.outFilters], n2 = (this._outputShape[0] + 2 * this.outPadding) * (this._outputShape[1] + 2 * this.outPadding) * this.outFilters, Be.debug("[Conv2dEntry_Upscale]: Creating output buffer of size", n2), i2 = r2.dataType === Un.F32 ? 4 : 2, this._configuration = r2, this._activation = r2.activation, this._outputs = new Nn(t4, n2 * i2, this.hostReadableOutput ? On.DeviceToHost : On.Default), o2 = this._createShader(r2.expectedInputType), this._sampler = t4.device.createSampler({ magFilter: "linear", minFilter: "linear" }), e6.next = 1, t4.createShaderModule(o2);
                    case 1:
                      if (a2 = e6.sent, s2 = a2.module, !(u2 = a2.err) && s2) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", u2 || new Sn("Conv2dEntry_Upscale", Tn.PipelineCreationFailed, "Failed to create shader module", true));
                    case 2:
                      return this._pipeline = t4.device.createComputePipeline({ layout: "auto", compute: { module: s2, entryPoint: "main" }, label: "conv2d_entry_upscale" }), e6.next = 3, t4.device.popErrorScope();
                    case 3:
                      if (!(c2 = e6.sent)) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", new Sn("Conv2dEntry_Upscale", Tn.PipelineCreationFailed, "Error creating compute pipeline: " + c2.message, true));
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.setWeights = function(e4) {
              this._weights = e4;
            }, t3.setBiases = function(e4) {
              this._biases = e4;
            }, t3.setInputs = function(e4) {
              this._inputs = e4;
            }, t3.setQuantOffsets = function(e4) {
            }, t3.getOutputs = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", this._outputs);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.close = function() {
              var e4;
              this._weights.close(), null == (e4 = this._biases) || e4.close(), this._outputs.close();
            }, t3.createFinalizedPass = function(e4, t4, r2) {
              try {
                var n2, i2 = { label: "conv2d_entry_upscale" }, o2 = this._pipeline.getBindGroupLayout(0);
                if (this._inputs instanceof GPUTexture) n2 = this._inputs.createView();
                else if (this._inputs instanceof VideoFrame) n2 = e4.device.importExternalTexture({ source: this._inputs });
                else {
                  if (!(this._inputs instanceof GPUExternalTexture)) return Be.error("Conv2d_entry_upscale invalid input type"), { err: new Sn("Conv2d_Entry_Upscale", Tn.BadParameters, "Input type is not one of GPUTexture, GPUExternalTexture, VideoFrame", true) };
                  n2 = this._inputs;
                }
                var a2 = [{ binding: 0, resource: { buffer: this._outputs.deviceBuffer } }, { binding: 1, resource: n2 }, { binding: 2, resource: this._sampler }, { binding: 3, resource: { buffer: this._weights.deviceBuffer } }];
                this._biases && a2.push({ binding: 4, resource: { buffer: this._biases.deviceBuffer } });
                var s2 = e4.device.createBindGroup({ layout: o2, entries: a2 });
                r2 && (i2.timestampWrites = { querySet: r2, beginningOfPassWriteIndex: 0, endOfPassWriteIndex: 1 });
                var u2 = t4.beginComputePass(i2);
                return u2.setPipeline(this._pipeline), u2.setBindGroup(0, s2), u2.dispatchWorkgroups(Math.ceil(this._outputShape[1] / 12), Math.ceil(this._outputShape[0] / 12), 1), u2.end(), { pass: u2 };
              } catch (e5) {
                return { err: new Sn("Conv2d_Entry_Upscale", Tn.InferenceFailed, "Failed to create finalized pass: " + ((null == e5 ? void 0 : e5.message) || "unknown"), true) };
              }
            }, t3._createShader = function(e4) {
              var t4 = e4 === Rn.External ? "texture_external" : "texture_2d<fT>", r2 = this._configuration.dataType, n2 = "sum;";
              this._activation && (n2 = this._activation.getWGSL("x", r2) + ";");
              var i2 = 4, o2 = "";
              return this._biases && (o2 = "@group(0) @binding(" + i2 + ") var<storage, read> biases: array<fT, M * N>;", i2++), "\n        " + (this._configuration.dataType === Un.F16 ? "enable f16;" : "") + "\n        const GROUP_SIDE: u32 = 12;\n        const GROUP_LIMIT: u32 = 14;\n        const GROUP_THREADS: u32 = GROUP_SIDE * GROUP_SIDE;\n        const WARP_SIZE = 32;\n        const IN_FILTERS: u32 = " + this.inFilters + ";\n        const OUT_FILTERS: u32 = " + this.outFilters + ";\n        const OUT_PADDING: u32 = " + this.outPadding + ";\n        const IN_PADDING: u32 = 1;\n\n        const TOTAL_FILTERS = IN_FILTERS * OUT_FILTERS;\n\n        const TILE_OFFSET: u32 = (GROUP_LIMIT - GROUP_SIDE) / 2;\n        const KERNEL_SIZE: u32 = 3;\n        const OUT_WIDTH: u32 = " + this._outputShape[1] + ";\n        const OUT_HEIGHT: u32 = " + this._outputShape[0] + ";\n        const Y_COEF = vec3<fT>(0.299, 0.587, 0.114);\n\n        alias fT = " + this._configuration.dataType + ";\n\n        @group(0) @binding(0) var<storage, read_write> outputBuffer: array<fT, (OUT_HEIGHT + OUT_PADDING * 2) * (OUT_WIDTH + OUT_PADDING * 2) * OUT_FILTERS>;\n        @group(0) @binding(1) var inputTexture: " + t4 + ";\n        @group(0) @binding(2) var inputSampler: sampler;\n        @group(0) @binding(3) var<storage, read> weightsBuffer: array<fT, KERNEL_SIZE * KERNEL_SIZE * TOTAL_FILTERS>;\n\n        " + o2 + "\n\n        var<workgroup> weights: array<fT, KERNEL_SIZE * KERNEL_SIZE * TOTAL_FILTERS>;\n        var<workgroup> scratch1: array<fT, GROUP_LIMIT*GROUP_LIMIT>; // Handles input to convolution\n\n        fn activate(x: fT) -> fT {\n            return " + n2 + ";\n        }\n\n        @compute @workgroup_size(GROUP_SIDE,GROUP_SIDE,1)\n        fn main(@builtin(global_invocation_id) global_id: vec3<u32>,\n                @builtin(local_invocation_index) tid: u32,\n                @builtin(local_invocation_id) thread_id: vec3<u32>,\n                @builtin(workgroup_id) group_id: vec3<u32>) {\n            let texSize = vec2<f32>(f32(OUT_WIDTH), f32(OUT_HEIGHT));\n            let texelSize = 1.0 / texSize;\n            let cRow = group_id.y * GROUP_SIDE;\n            let cCol = group_id.x * GROUP_SIDE ;\n\n            // 1. load weights\n            for (var i: u32 = 0 ; i < (TOTAL_FILTERS*KERNEL_SIZE*KERNEL_SIZE+GROUP_THREADS-1)/GROUP_THREADS; i++) {\n                let idx: u32 = i * GROUP_THREADS + tid;\n                if (idx < TOTAL_FILTERS*KERNEL_SIZE*KERNEL_SIZE) {\n                    weights[idx] = weightsBuffer[idx];\n                }\n            }\n\n            // 2. load pixels from input texture into shared scratch\n            if (thread_id.x * 2 < GROUP_LIMIT && thread_id.y * 2 < GROUP_LIMIT) {\n                let sRow = i32(cRow) - i32(TILE_OFFSET);\n                let sCol = i32(cCol) - i32(TILE_OFFSET);\n                let lRow = i32(thread_id.y * 2) + sRow;\n                let lCol = i32(thread_id.x * 2) + sCol;\n                var s: array<vec3<fT>, 4>;\n                let curPos = vec2<f32>(f32(lCol), f32(lRow)) * texelSize;\n                let cInc = vec2<f32>(texelSize.x, 0.0);\n                let rInc = vec2<f32>(0.0, texelSize.y);\n\n                // Collect the pixels this thread should collect, storing in registers\n                s[0] = vec3<fT>(textureSampleBaseClampToEdge(inputTexture, inputSampler, curPos).rgb);\n                s[1] = vec3<fT>(textureSampleBaseClampToEdge(inputTexture, inputSampler, curPos+cInc).rgb);\n                s[2] = vec3<fT>(textureSampleBaseClampToEdge(inputTexture, inputSampler, curPos+rInc).rgb);\n                s[3] = vec3<fT>(textureSampleBaseClampToEdge(inputTexture, inputSampler, curPos+texelSize).rgb);\n\n                // Do YUV conversion and store in workgroup memory.\n                // Doing the YUV conversion here instead of when storing to registers seems to be more performant (on M2 macbook anyway)\n                //\n                let row = thread_id.y * 2;\n                let col = thread_id.x * 2;\n\n                // Convert RGB to YUV and compress from full range to video range [16-236]. Not doing this will result in artifacts.\n                scratch1[row * GROUP_LIMIT + col] = dot(s[0] * 0.8627 + 0.0627, Y_COEF);\n                if (col+1 < GROUP_LIMIT) {\n                    scratch1[row * GROUP_LIMIT + col + 1] = dot(s[1] * 0.8627 + 0.0627, Y_COEF);\n                }\n                if (row+1 < GROUP_LIMIT) {\n                    scratch1[(row+1) * GROUP_LIMIT + col] = dot(s[2] * 0.8627 + 0.0627, Y_COEF);\n                    if (col+1 < GROUP_LIMIT) {\n                        scratch1[(row+1) * GROUP_LIMIT + col + 1] = dot(s[3] * 0.8627 + 0.0627, Y_COEF);\n                    }\n                }\n            }\n            workgroupBarrier();\n\n            // Each thread will perform OUT_FILTERS convolutions\n            var centers: array<u32, OUT_FILTERS>;\n            var results: array<fT, OUT_FILTERS>;\n            for (var i: u32 = 0; i < OUT_FILTERS; i++) {\n                let z = thread_id.z + i;\n                // let y = thread_id.y;\n                // let x = thread_id.x;\n                centers[i] = z;//vec3<u32>(x,y,z);\n            }\n            {\n\n                let row = thread_id.y + 1;\n                let col = thread_id.x + 1;\n\n                // Load values. There will probably be bank conflicts here that we can optimize in the future.\n                // TODO: Batch this into array<fT,9*4>;\n                var v: array<fT, 9>;\n                v[0] = scratch1[(row-1) * GROUP_LIMIT + (col-1)];\n                v[3] = scratch1[(row) * GROUP_LIMIT + (col-1)];\n                v[6] = scratch1[(row+1) * GROUP_LIMIT + (col-1)];\n                v[1] = scratch1[(row-1) * GROUP_LIMIT + (col)];\n                v[4] = scratch1[(row) * GROUP_LIMIT + (col)];\n                v[7] = scratch1[(row+1) * GROUP_LIMIT + (col)];\n                v[2] = scratch1[(row-1) * GROUP_LIMIT + (col+1)];\n                v[5] = scratch1[(row) * GROUP_LIMIT + (col+1)];\n                v[8] = scratch1[(row+1) * GROUP_LIMIT + (col+1)];\n                for (var i: u32 = 0; i < OUT_FILTERS; i++) {\n                    // Load weights\n                    var w: array<fT, 9>;\n                    let off = centers[i] * KERNEL_SIZE * KERNEL_SIZE;\n                    for (var j: u32 = 0; j < KERNEL_SIZE * KERNEL_SIZE; j++) {\n                        w[j] = weights[off + j];\n                    }\n\n                    // Do dots\n                    var sum: fT = 0.0;\n                    sum += v[0] * w[0];\n                    sum += v[1] * w[1];\n                    sum += v[2] * w[2];\n                    sum += v[3] * w[3];\n                    sum += v[4] * w[4];\n                    sum += v[5] * w[5];\n                    sum += v[6] * w[6];\n                    sum += v[7] * w[7];\n                    sum += v[8] * w[8];\n                    results[i] = activate(sum);\n                }\n            }\n\n            // write results\n            for (var i: u32 = 0; i < OUT_FILTERS; i++) {\n                let x = thread_id.x + cCol;\n                let y = thread_id.y + cRow;\n                let z = centers[i];\n                // reliniarize\n                let idx = (y+OUT_PADDING) * (OUT_WIDTH + OUT_PADDING * 2) * OUT_FILTERS + x * OUT_FILTERS + z;\n                outputBuffer[idx] = results[i];\n            }\n        }\n        ";
            }, e3;
          })(), Bn = (function() {
            function e3(e4, t4, r2) {
              void 0 === r2 && (r2 = false), this.inFilters = e4, this.outFilters = t4, this.hostReadableOutput = r2, this._configuration = void 0, this._activation = void 0, this._weights = void 0, this._biases = void 0, this._inputs = void 0, this._outputs = void 0, this._pipeline = void 0, this._inputShape = void 0, this._outputShape = void 0;
            }
            var t3 = e3.prototype;
            return t3.configure = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2, c2, l2, d2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.prev = 0, t4.device.pushErrorScope("validation"), n2 = Math.floor(1), this._outputShape = [r2.inputShape[0] - n2 + 1, r2.inputShape[1] - n2 + 1, this.outFilters], this._inputShape = r2.inputShape, i2 = (this._outputShape[0] + 2) * (this._outputShape[1] + 2) * this.outFilters, Be.debug("GPURenderPipeline hidden output shape: " + this._outputShape + ", size: " + i2 + " hostReadable " + this.hostReadableOutput), o2 = r2.dataType === Un.F32 ? 4 : 2, this._configuration = r2, this._activation = r2.activation, this._outputs = new Nn(t4, i2 * o2, this.hostReadableOutput ? On.DeviceToHost : On.Default), a2 = this.createShader(), e6.next = 1, t4.createShaderModule(a2);
                    case 1:
                      if (s2 = e6.sent, u2 = s2.module, !(c2 = s2.err) && u2) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", new Sn("Conv2d_3x3s1p1", Tn.PipelineCreationFailed, "Pipeline creation failed: " + (null == c2 ? void 0 : c2.message), true));
                    case 2:
                      return this._pipeline = t4.device.createComputePipeline({ layout: "auto", compute: { module: u2, entryPoint: "main" }, label: "conv2d_entry_upscale" }), e6.next = 3, t4.device.popErrorScope();
                    case 3:
                      if (!(l2 = e6.sent)) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", new Sn("Conv2d_3x3s1p1", Tn.PipelineCreationFailed, "Pipeline creation failed: " + (null == l2 ? void 0 : l2.message), true));
                    case 4:
                      e6.next = 6;
                      break;
                    case 5:
                      return e6.prev = 5, d2 = e6.catch(0), e6.abrupt("return", new Sn("Conv2d_3x3s1p1", Tn.PipelineCreationFailed, "Pipeline creation failed: " + (null == d2 ? void 0 : d2.message), true));
                    case 6:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[0, 5]]);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.close = function() {
              var e4;
              this._weights.close(), null == (e4 = this._biases) || e4.close(), this._outputs.close();
            }, t3.setWeights = function(e4) {
              this._weights = e4;
            }, t3.setBiases = function(e4) {
              this._biases = e4;
            }, t3.setInputs = function(e4) {
              this._inputs = e4;
            }, t3.getOutputs = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", this._outputs);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.createFinalizedPass = function(e4, t4, r2) {
              if (!(this._inputs instanceof Nn)) return { err: new Sn("Conv2d_3x3s1p1", Tn.BadParameters, "ModelLayerInput is not a Buffer", true) };
              try {
                var n2 = { label: "conv2d_mid_basic" }, i2 = this._pipeline.getBindGroupLayout(0), o2 = [{ binding: 0, resource: { buffer: this._outputs.deviceBuffer } }, { binding: 1, resource: { buffer: this._inputs.deviceBuffer } }, { binding: 2, resource: { buffer: this._weights.deviceBuffer } }];
                this._biases && o2.push({ binding: 3, resource: { buffer: this._biases.deviceBuffer } });
                var a2 = e4.device.createBindGroup({ layout: i2, entries: o2 });
                r2 && (n2.timestampWrites = { querySet: r2, beginningOfPassWriteIndex: 0, endOfPassWriteIndex: 1 });
                var s2 = t4.beginComputePass(n2);
                return s2.setPipeline(this._pipeline), s2.setBindGroup(0, a2), s2.dispatchWorkgroups(Math.ceil(this._outputShape[1] / 12), Math.ceil(this._outputShape[0] / 8), this._outputShape[2] / 8), s2.end(), { pass: s2 };
              } catch (e5) {
                return { err: new Sn("Conv2d_3x3s1p1", Tn.InferenceFailed, "Layer pass failed: " + ((null == e5 ? void 0 : e5.message) || "unknown"), true) };
              }
            }, t3.createShader = function() {
              var e4 = this._configuration.dataType, t4 = "sum;";
              this._activation && (t4 = this._activation.getWGSL("x", e4) + ";");
              var r2 = "";
              return this._biases && (r2 = "@group(0) @binding(3) var<storage, read> biases: array<fT, M * N>;"), "\n            " + (this._configuration.dataType === Un.F16 ? "enable f16;" : "") + "\n            const IN_FILTERS: u32 = " + this.inFilters + ";\n            const OUT_FILTERS: u32 = " + this.outFilters + ";\n            const TOTAL_FILTERS: u32 = IN_FILTERS * OUT_FILTERS;\n            const OUT_WIDTH: u32 = " + this._outputShape[1] + ";\n            const OUT_HEIGHT: u32 = " + this._outputShape[0] + ";\n            const IN_WIDTH: u32 = " + this._inputShape[1] + ";\n            const IN_HEIGHT: u32 = " + this._inputShape[0] + ";\n            const PADDING: u32 = 1;\n            const KERNEL_SIZE: u32 = 3;\n            const GROUP_SIDE_W: u32 = 12;\n            const GROUP_SIDE_H: u32 = 8;\n            const GROUP_DEPTH: u32 = 8;\n            const THREAD_DEPTH: u32 = 8;\n            const GROUP_LIMIT_W: u32 = GROUP_SIDE_W + PADDING * 2;\n            const GROUP_LIMIT_H: u32 = GROUP_SIDE_H + PADDING * 2;\n            const GROUP_THREADS: u32 = GROUP_SIDE_W * GROUP_SIDE_H;\n\n            const WARP_SIZE: u32 = 32;\n            alias fT = " + this._configuration.dataType + ";\n            @group(0) @binding(0) var<storage, read_write> outputBuffer: array<fT, (OUT_HEIGHT + PADDING * 2) * (OUT_WIDTH + PADDING * 2) * OUT_FILTERS>;\n            @group(0) @binding(1) var<storage, read> inputBuffer: array<fT, (IN_HEIGHT + PADDING * 2) * (IN_WIDTH + PADDING * 2) * IN_FILTERS>;\n            @group(0) @binding(2) var<storage, read> weightsBuffer: array<fT, TOTAL_FILTERS * KERNEL_SIZE * KERNEL_SIZE>;\n            " + r2 + "\n            var<workgroup> weights: array<fT, KERNEL_SIZE * KERNEL_SIZE * TOTAL_FILTERS>;\n            var<workgroup> inputs: array<fT, GROUP_LIMIT_W * GROUP_LIMIT_H * IN_FILTERS>;\n\n            fn activate(x: fT) -> fT {\n                return " + t4 + ";\n            }\n\n            @compute @workgroup_size(GROUP_SIDE_W, GROUP_SIDE_H, GROUP_DEPTH/THREAD_DEPTH)\n            fn main(@builtin(global_invocation_id) global_id: vec3<u32>,\n                    @builtin(local_invocation_index) tid: u32,\n                    @builtin(local_invocation_id) thread_id: vec3<u32>,\n                    @builtin(workgroup_id) group_id: vec3<u32>) {\n\n                let tileCol = group_id.x * GROUP_SIDE_W ;\n                let tileRow = group_id.y * GROUP_SIDE_H ;\n\n                // Load weights\n                for (var i: u32 = 0 ; i < ((TOTAL_FILTERS*KERNEL_SIZE*KERNEL_SIZE+GROUP_THREADS-1)/GROUP_THREADS); i++) {\n                    let idx: u32 = i * GROUP_THREADS + tid;\n                    if (idx < TOTAL_FILTERS*KERNEL_SIZE*KERNEL_SIZE) {\n                        weights[idx] = weightsBuffer[idx];\n                    }\n                }\n\n                // Load activations. This will be GROUP_LIMIT*GROUP_LIMIT*IN_FILTERS in size.\n                // Buffers are in (H,W,C) format\n\n                if (thread_id.x * 2 < GROUP_LIMIT_W && thread_id.y * 2 < GROUP_LIMIT_H) {\n                    for (var c: u32 = 0; c < IN_FILTERS; c++) {\n                        for (var n: u32 = 0; n < 2; n++) {\n                            for (var m: u32 = 0; m < 2; m++) {\n                                let sCol: u32 = (thread_id.x*2+m) * IN_FILTERS + c;\n                                let sRow: u32 = (thread_id.y*2+n) * IN_FILTERS * GROUP_LIMIT_W;\n                                let gIdx: u32 = (tileRow + thread_id.y*2 + n) * IN_FILTERS * (IN_WIDTH + PADDING * 2) + (tileCol + thread_id.x*2 + m) * IN_FILTERS + c;\n                                inputs[sRow + sCol] = inputBuffer[gIdx];\n                            }\n                        }\n                    }\n                }\n                workgroupBarrier();\n                var centers: array<vec3<u32>, THREAD_DEPTH>;\n                var results: array<fT, THREAD_DEPTH>;\n                for (var i: u32 = 0; i < THREAD_DEPTH; i++) {\n                    let z = group_id.z * GROUP_DEPTH + thread_id.z * THREAD_DEPTH + i;\n                    let y = thread_id.y;\n                    let x = thread_id.x;\n                    centers[i] = vec3<u32>(x,y,z);\n                    results[i] = 0.0;\n                }\n                for (var z: u32 = 0; z < IN_FILTERS; z++) {\n                    let row = thread_id.y + PADDING;\n                    let col = thread_id.x + PADDING;\n                    let c: u32 = (tid + z) % IN_FILTERS;\n                    // Load values. There will probably be bank conflicts here that we can optimize in the future.\n                    var v: array<fT, 9>;\n                    v[0] = inputs[(row-1) * GROUP_LIMIT_W * IN_FILTERS + (col-1) * IN_FILTERS + c];\n                    v[3] = inputs[(row) * GROUP_LIMIT_W * IN_FILTERS + (col-1) * IN_FILTERS + c];\n                    v[6] = inputs[(row+1) * GROUP_LIMIT_W * IN_FILTERS + (col-1) * IN_FILTERS + c];\n                    v[1] = inputs[(row-1) * GROUP_LIMIT_W * IN_FILTERS + (col) * IN_FILTERS + c];\n                    v[4] = inputs[(row) * GROUP_LIMIT_W * IN_FILTERS + (col) * IN_FILTERS + c];\n                    v[7] = inputs[(row+1) * GROUP_LIMIT_W * IN_FILTERS + (col) * IN_FILTERS + c];\n                    v[2] = inputs[(row-1) * GROUP_LIMIT_W * IN_FILTERS + (col+1) * IN_FILTERS + c];\n                    v[5] = inputs[(row) * GROUP_LIMIT_W * IN_FILTERS + (col+1) * IN_FILTERS + c];\n                    v[8] = inputs[(row+1) * GROUP_LIMIT_W * IN_FILTERS + (col+1) * IN_FILTERS + c];\n\n                    // Do dots\n                    for (var i: u32 = 0; i < THREAD_DEPTH; i++) {\n                        let weightBase = centers[i].z * IN_FILTERS * KERNEL_SIZE * KERNEL_SIZE ;\n                        var w: array<fT, 9>;\n                        let off = weightBase + c * KERNEL_SIZE * KERNEL_SIZE;\n                        for (var j: u32 = 0; j < KERNEL_SIZE * KERNEL_SIZE; j++) {\n                            w[j] = weights[off + j];\n                        }\n\n                        var sum: fT = 0.0;\n                        sum += v[0] * w[0];\n                        sum += v[1] * w[1];\n                        sum += v[2] * w[2];\n                        sum += v[3] * w[3];\n                        sum += v[4] * w[4];\n                        sum += v[5] * w[5];\n                        sum += v[6] * w[6];\n                        sum += v[7] * w[7];\n                        sum += v[8] * w[8];\n                        results[i] += sum;\n                    }\n                }\n\n                // write results\n                for (var i: u32 = 0; i < GROUP_DEPTH; i++) {\n                    let x = centers[i].x + PADDING;\n                    let y = centers[i].y + PADDING;\n                    let z = centers[i].z;\n\n                    let outIdx = (tileRow + y) * (OUT_WIDTH + PADDING * 2) * OUT_FILTERS + (tileCol + x) * OUT_FILTERS + z ;\n                    if (tileRow + y < (OUT_HEIGHT - PADDING) && tileCol + x < (OUT_WIDTH - PADDING)) {\n                        outputBuffer[outIdx] = activate(results[i]);\n                    }\n                }\n            };\n        ";
            }, e3;
          })(), Gn = (function(e3) {
            return e3[e3.Bits16 = 0] = "Bits16", e3[e3.Bits32 = 1] = "Bits32", e3[e3.Vec2_Bits16 = 2] = "Vec2_Bits16", e3[e3.Vec4_Bits16 = 3] = "Vec4_Bits16", e3[e3.Vec2_Bits32 = 4] = "Vec2_Bits32", e3[e3.Vec4_Bits32 = 5] = "Vec4_Bits32", e3;
          })({});
          function jn(e3) {
            switch (e3) {
              case 16:
                return Gn.Vec4_Bits32;
              case 8:
                return Gn.Vec2_Bits32;
              case 4:
                return Gn.Bits32;
              case 2:
                return Gn.Bits16;
            }
            return Gn.Bits32;
          }
          function Hn(e3) {
            switch (e3) {
              case Gn.Bits16:
              case Gn.Vec2_Bits16:
              case Gn.Vec4_Bits16:
                return 2;
              default:
                return 4;
            }
          }
          function Wn(e3) {
            switch (e3) {
              case Gn.Bits16:
              case Gn.Bits32:
                return 1;
              case Gn.Vec2_Bits16:
              case Gn.Vec2_Bits32:
                return 2;
              case Gn.Vec4_Bits16:
              case Gn.Vec4_Bits32:
                return 4;
            }
          }
          function Kn(e3) {
            return Hn(e3) * Wn(e3);
          }
          function zn(e3, t3, r2) {
            void 0 === r2 && (r2 = false);
            var n2 = Kn(e3), i2 = 2 === Hn(e3) ? "f16" : r2 ? "f32" : "u32", o2 = "";
            switch (Wn(e3)) {
              case 4:
                o2 = "vec4<" + i2 + ">";
                break;
              case 2:
                o2 = "vec2<" + i2 + ">";
                break;
              default:
                o2 = i2;
            }
            return n2 === t3 ? o2 : "array<" + o2 + ", " + Math.floor(t3 / n2) + ">";
          }
          function qn(e3, t3, r2, n2, i2, o2, a2, s2, u2) {
            var c2 = Kn(u2.bufferMemoryLayout);
            if (1 === n2 || t3 === e3 && r2 === e3) return Yn(e3, n2, i2, o2, a2, s2, u2);
            if (e3 >= u2.numberThreads * c2) {
              for (var l2 = "", d2 = 0; d2 < n2; d2++) l2 += "{\n                let src_off_" + d2 + " = " + i2 + " + " + d2 * t3 + ";\n                let dst_off_" + d2 + " = " + o2 + " + " + d2 * r2 + ";\n                " + Yn(e3, 1, "src_off_" + d2, "dst_off_" + d2, a2, s2, u2) + "\n            }";
              return l2;
            }
            return (function(e4, t4, r3, n3, i3, o3, a3, s3, u3) {
              var c3 = Kn(u3.bufferMemoryLayout), l3 = (function(e5, t5) {
                return (function(e6, t6, r4) {
                  var n4 = Math.ceil(e6 / (r4.subgroupSize * t6));
                  return { subgroupsPerLoadgroup: n4, loadgroups: Math.floor(r4.numberThreads / (r4.subgroupSize * n4)) };
                })(e5, Kn(t5.bufferMemoryLayout), t5);
              })(e4, u3), d3 = l3.subgroupsPerLoadgroup, f2 = l3.loadgroups, h2 = Math.floor(n3 / f2), p2 = n3 % f2, v2 = "{\n";
              v2 += (function(e5, t5) {
                var r4 = "";
                return (r4 += "let loadgroupId = i32(" + t5.subgroupIdName + ") / " + e5 + ";\n") + "let loadgroupTid = i32(" + t5.threadIdName + ") % (" + e5 * t5.subgroupSize + ");\n";
              })(d3, u3), v2 += "if (loadgroupTid < " + e4 / c3 + ") {";
              for (var g2 = 0; g2 < h2; g2++) {
                var m2 = Qn(0, 0, r3, h2, 0, "loadgroupId", "loadgroupTid", 0, o3, g2, u3), y2 = Qn(0, 0, t4, h2, 0, "loadgroupId", "loadgroupTid", 0, i3, g2, u3);
                v2 += "{\n            " + m2.registers + "\n            " + y2.registers + "\n            " + s3 + m2.indices + " = " + a3 + y2.indices + ";\n        }";
              }
              if (p2 > 0) {
                var b2 = Qn(0, 0, r3, 1, 0, "loadgroupId", "loadgroupTid", h2 * f2 * r3, o3, 0, u3), E2 = Qn(0, 0, t4, 1, 0, "loadgroupId", "loadgroupTid", h2 * f2 * t4, i3, 0, u3);
                v2 += "\n        if (loadgroupId < " + p2 + ") {\n            " + b2.registers + "\n            " + E2.registers + "\n            " + s3 + b2.indices + " = " + a3 + E2.indices + ";\n        }";
              }
              return v2 += "}", v2 += "}\n";
            })(e3, t3, r2, n2, i2, o2, a2, s2, u2);
          }
          function Qn(e3, t3, r2, n2, i2, o2, a2, s2, u2, c2, l2) {
            l2.subgroupThreadIdName, 0 === n2 && (l2.threadIdName, l2.numberThreads, l2.subgroupSize);
            var d2 = l2.bufferMemoryLayout, f2 = (Hn(d2), Wn(d2), Kn(d2)), h2 = "[";
            return h2 += "(" + u2 + " / " + f2 + ") + " + s2 / f2 + " + ", h2 += "(" + c2 + " + " + n2 + " * " + o2 + ") * " + r2 / f2 + " +", h2 += "(" + a2 + ")", { registers: "", indices: h2 += "]" };
          }
          function Yn(e3, t3, r2, n2, i2, o2, a2) {
            var s2 = e3 * t3, u2 = Kn(a2.bufferMemoryLayout), c2 = Math.floor(s2 / (u2 * a2.numberThreads)), l2 = s2 % (u2 * a2.numberThreads) / u2, d2 = "{\n";
            d2 += "let loadgroupId = 1;\n", d2 += "let loadgroupTid = i32(" + a2.threadIdName + ");\n";
            for (var f2 = 0; f2 < c2; f2++) {
              var h2 = Qn(0, a2.numberThreads, u2 * a2.numberThreads, 0, 0, "loadgroupId", "loadgroupTid", 0, n2, f2, a2), p2 = Qn(0, a2.numberThreads, u2 * a2.numberThreads, 0, 0, "loadgroupId", "loadgroupTid", 0, r2, f2, a2);
              d2 += "{", d2 += "" + h2.registers, d2 += "" + p2.registers, d2 += "" + o2 + h2.indices + " = " + i2 + p2.indices + ";", d2 += "}\n";
            }
            if (l2 > 0) {
              d2 += "\n              if (" + a2.threadIdName + " < " + l2 + ") {\n";
              var v2 = Qn(0, 0, l2 * u2, 0, 0, "loadgroupId", "loadgroupTid", c2 * u2 * a2.numberThreads, n2, 0, a2), g2 = Qn(0, 0, l2 * u2, 0, 0, "loadgroupId", "loadgroupTid", c2 * u2 * a2.numberThreads, r2, 0, a2);
              d2 += v2.registers, d2 += g2.registers, d2 += "" + o2 + v2.indices + " = " + i2 + g2.indices + ";", d2 += "}";
            }
            return d2 + "}\n";
          }
          var Zn = (function() {
            function e3(e4, t4, r2) {
              void 0 === r2 && (r2 = false), this.inChannels = e4, this.outChannels = t4, this.hostReadableOutput = r2, this._configuration = void 0, this._activation = void 0, this._weights = void 0, this._biases = void 0, this._inputs = void 0, this._outputs = void 0, this._pipeline = void 0, this._inputShape = void 0, this._outputShape = void 0;
            }
            var t3 = e3.prototype;
            return t3.configure = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2, c2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return t4.device.pushErrorScope("validation"), this._outputShape = [r2.inputShape[0] - 1 + 1, r2.inputShape[1] - 1 + 1, this.outChannels], this._inputShape = r2.inputShape, n2 = (this._outputShape[0] + 2) * (this._outputShape[1] + 2) * this.outChannels, Be.debug("[Conv2d_igemm_f16_Nx3x3x8]: Hidden output shape: " + this._outputShape + ", size: " + n2 + " hostReadable " + this.hostReadableOutput), i2 = r2.dataType === Un.F32 ? 4 : 2, this._configuration = r2, this._activation = r2.activation, this._outputs = new Nn(t4, n2 * i2, this.hostReadableOutput ? On.DeviceToHost : On.Default), o2 = this._createShader(), Be.debug("[Conv2d_igemm_f16_Nx3x3x8]: Shader", o2), e6.next = 1, t4.createShaderModule(o2);
                    case 1:
                      if (a2 = e6.sent, s2 = a2.module, !(u2 = a2.err) && s2) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", u2 || new Sn("Conv2d_igemm_f16_Nx3x3x8", Tn.PipelineCreationFailed, "Failed to create shader module", true));
                    case 2:
                      return this._pipeline = t4.device.createComputePipeline({ layout: "auto", compute: { module: s2, entryPoint: "main" }, label: "conv2d_igemm_f16_Nx3x3x8" }), e6.next = 3, t4.device.popErrorScope();
                    case 3:
                      if (!(c2 = e6.sent)) {
                        e6.next = 4;
                        break;
                      }
                      return e6.abrupt("return", new Sn("conv2d_igemm_f16_Nx3x3x8", Tn.PipelineCreationFailed, "Failed to create compute pipeline: " + c2.message, true));
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.setWeights = function(e4) {
              this._weights = e4;
            }, t3.setBiases = function(e4) {
              this._biases = e4;
            }, t3.setInputs = function(e4) {
              this._inputs = e4;
            }, t3.setQuantOffsets = function(e4) {
            }, t3.getOutputs = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", this._outputs);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.close = function() {
              var e4;
              this._weights.close(), null == (e4 = this._biases) || e4.close(), this._outputs.close();
            }, t3.createFinalizedPass = function(e4, t4, r2) {
              if (!(this._inputs instanceof Nn)) return { err: new Sn("Conv2d_igemm_f16_Nx3x3x8", Tn.BadParameters, "ModelLayerInput is not a Buffer", true) };
              try {
                var n2 = { label: "conv2d_mid_basic" }, i2 = this._pipeline.getBindGroupLayout(0), o2 = [{ binding: 0, resource: { buffer: this._outputs.deviceBuffer } }, { binding: 1, resource: { buffer: this._inputs.deviceBuffer } }, { binding: 2, resource: { buffer: this._weights.deviceBuffer } }];
                this._biases && o2.push({ binding: 3, resource: { buffer: this._biases.deviceBuffer } });
                var a2 = e4.device.createBindGroup({ layout: i2, entries: o2 });
                r2 && (n2.timestampWrites = { querySet: r2, beginningOfPassWriteIndex: 0, endOfPassWriteIndex: 1 });
                var s2 = t4.beginComputePass(n2);
                return s2.setPipeline(this._pipeline), s2.setBindGroup(0, a2), s2.dispatchWorkgroups(Math.ceil(this._outputShape[1] / 16), Math.ceil(this._outputShape[0] / 8), 1), s2.end(), { pass: s2 };
              } catch (e5) {
                return { err: new Sn("Conv2d_igemm_f16_Nx3x3x8", Tn.InferenceFailed, "Finalized pass failed: " + ((null == e5 ? void 0 : e5.message) || "unknown"), true) };
              }
            }, t3._createShader = function() {
              var e4 = this, t4 = this._configuration.dataType, r2 = "sum;";
              this._activation && (r2 = this._activation.getWGSL("x", t4) + ";");
              var n2 = "";
              this._biases && (n2 = "@group(0) @binding(3) var<storage, read> biases: array<fT, M * N>;");
              var i2 = { bufferMemoryLayout: jn(16), numberThreads: 64, subgroupSize: 32, threadIdName: "tid", subgroupThreadIdName: "wtid", subgroupIdName: "wid", subgroupSizeName: "32" }, o2 = Kn(i2.bufferMemoryLayout), a2 = (this._inputShape[0] + 2) * (this._inputShape[1] + 2) * this.inChannels * 2, s2 = this.inChannels * this.outChannels * 3 * 3 * 2, u2 = 18 * this.inChannels * 10 * 2, c2 = Wn(i2.bufferMemoryLayout), l2 = o2 / (2 * c2), d2 = Math.log2(c2), f2 = Math.log2(l2);
              return "\n        " + (this._configuration.dataType === Un.F16 ? "enable f16;" : "") + "\n        // Load Width: 16\n        const IN_CHANNELS: i32 = " + this.inChannels + ";\n        const OUT_CHANNELS: i32 = " + this.outChannels + ";\n        const PACKED_IN_CHANNELS: i32 = IN_CHANNELS/2;\n        const PACKED_OUT_CHANNELS: i32 = OUT_CHANNELS/2;\n        const KERNEL_SIZE: i32 = 3;\n\n        const TILE_IN_W: i32 = 18;\n        const TILE_IN_H: i32 = 10;\n        const TILE_OUT_W: i32 = 16;\n        const TILE_OUT_H: i32 = 8;\n        const TILE_OUT_DEPTH: i32 = OUT_CHANNELS;\n        const PACKED_TILE_OUT_DEPTH: i32 = PACKED_OUT_CHANNELS;\n\n        const IN_PADDING: i32 = 1;\n        const OUT_PADDING: i32 = 1;\n\n        const IN_WIDTH: i32 = " + this._inputShape[1] + ";\n        const IN_HEIGHT: i32 = " + this._inputShape[0] + ";\n        const OUT_WIDTH: i32 = " + this._outputShape[1] + ";\n        const OUT_HEIGHT: i32 = " + this._outputShape[0] + ";\n\n        const K: i32 = IN_CHANNELS;\n\n        alias fT = " + this._configuration.dataType + ";\n        @group(0) @binding(0) var<storage, read_write> outputBuffer: array<vec4<u32>, (OUT_HEIGHT + OUT_PADDING * 2) * (OUT_WIDTH + OUT_PADDING * 2)>;\n        @group(0) @binding(1) var<storage, read> inputBuffer: " + zn(i2.bufferMemoryLayout, a2) + ";\n        @group(0) @binding(2) var<storage, read> weightsBuffer: " + zn(i2.bufferMemoryLayout, s2) + ";\n        " + n2 + "\n\n        var<workgroup> S_WEIGHTS: " + zn(i2.bufferMemoryLayout, s2) + ";\n        var<workgroup> S_ACTIVATIONS: " + zn(i2.bufferMemoryLayout, u2) + ";\n\n        fn activate(x: fT) -> fT {\n            return " + r2 + ";\n        }\n\n        @compute @workgroup_size(64,1,1)\n        fn main(@builtin(global_invocation_id) global_id: vec3<u32>,\n                @builtin(local_invocation_index) tid: u32,\n                @builtin(local_invocation_id) thread_id: vec3<u32>,\n                @builtin(workgroup_id) group_id: vec3<u32>) {\n\n            let tileCol: i32 = i32(group_id.x) * TILE_OUT_W;\n            let tileRow: i32 = i32(group_id.y) * TILE_OUT_H;\n            let wid: i32 = i32(tid) >> " + Math.log2(32) + ";\n            let wtid: i32 = i32(tid) & 31;\n            let warpX: i32 = wid % 2;\n            let warpY: i32 = wid / 2;\n            let threadX: i32 = wtid % 8;\n            let threadY: i32 = wtid / 8;\n            \n            {\n                " + qn(s2, 0, 0, 1, "0", "0", "weightsBuffer", "S_WEIGHTS", i2) + "\n            }\n            \n            // Load activations to SMEM\n            {\n                let x: i32 = i32(group_id.x) * TILE_OUT_W;\n                let y: i32 = i32(group_id.y) * TILE_OUT_H;\n                let offsetBytes = (y * (IN_WIDTH+IN_PADDING*2) * IN_CHANNELS + x * IN_CHANNELS) * 2;\n                " + qn(18 * this.inChannels * 2, this.inChannels * (this._inputShape[1] + 2) * 2, 18 * this.inChannels * 2, 10, "offsetBytes", "0", "inputBuffer", "S_ACTIVATIONS", i2) + "\n            }\n            // Wait for memory operations to complete\n            workgroupBarrier();\n\n            var results: array<array<array<array<fT, 2>, PACKED_OUT_CHANNELS>, 1>, 2>;\n            for (var k_y: i32 = 0; k_y < 3; k_y ++) {\n            for (var k_x: i32 = 0; k_x < 3; k_x ++) {\n            for (var k: i32 = 0; k < K; k += 8) {\n                var weights: array<array<vec2<fT>, 4>, 4>;\n                var activations: array<array<array<vec2<fT>,4>, 1>, 2>;\n                " + Ln(2, "\n			", function(e5) {
                return Ln(1, "\n			", function(t5) {
                  return "\n                    {\n                        let idx: i32 = ((k_y + (warpY * 8) + (threadY * 2) + " + e5 + ") * TILE_IN_W * IN_CHANNELS\n                            + (k_x + (warpX * 8) + (threadX * 1) + " + t5 + ") * IN_CHANNELS\n                            + k) >> " + (f2 + d2) + ";\n                        let value = S_ACTIVATIONS[idx];\n                        " + Ln(c2, "\n", function(r3) {
                    return "activations[" + e5 + "][" + t5 + "][" + r3 + "] = vec2<f16>(unpack2x16float(value[" + r3 + "]));";
                  }) + "\n                    }";
                });
              }) + "\n                                \n                " + Ln(this.outChannels / 4, "", function(t5) {
                var r3 = 4 * t5;
                return "\n                    {\n                        let idx: i32 = (k_y * KERNEL_SIZE * IN_CHANNELS + k_x * IN_CHANNELS + k) >> " + (f2 + d2) + ";\n                        var values: array<vec4<u32>, 4>;\n                        " + Ln(4, "\n", function(t6) {
                  return "values[" + t6 + "] = S_WEIGHTS[" + (3 * (r3 + t6) * 3 * e4.inChannels >> f2 + d2) + " + idx];";
                }) + "\n                        " + Ln(4, "\n			", function(e5) {
                  return "\n                        {\n                            " + Ln(c2, "\n			", function(t6) {
                    return "weights[" + e5 + "][" + t6 + "] = vec2<f16>(unpack2x16float(values[" + e5 + "][" + t6 + "]));";
                  }) + "\n                        }";
                }) + "\n                    }\n                    " + Ln(2, "\n				", function(e5) {
                  return Ln(1, "\n				", function(t6) {
                    return Ln(4, "\n				", function(n3) {
                      var i3 = n3 % 2;
                      return Ln(8, "\n				", function(o3) {
                        var a3 = Math.floor(o3 / 2), s3 = o3 % 2, u3 = "results[" + e5 + "][" + t6 + "][" + Math.floor((n3 + r3) / 2) + "][" + i3 + "]";
                        return u3 + " = fma(activations[" + e5 + "][" + t6 + "][" + a3 + "][" + s3 + "], weights[" + n3 + "][" + a3 + "][" + s3 + "], " + u3 + ");";
                      });
                    });
                  });
                });
              }) + " // loop c_o\n            } // for (var k = ...)\n            } // for (var k_x = ...)\n            } // for (var k_y = ...)\n            // OK we now have all of our output channels for this pixel. Write to VRAM.\n            " + Ln(2, "\n			", function(t5) {
                return Ln(1, "\n			", function(r3) {
                  return "\n            {\n                var x: vec4<u32>;\n                " + Ln(e4.outChannels / 2, "", function(e5) {
                    return "\n                    results[" + t5 + "][" + r3 + "][" + e5 + "][0] = activate(results[" + t5 + "][" + r3 + "][" + e5 + "][0]);\n                    results[" + t5 + "][" + r3 + "][" + e5 + "][1] = activate(results[" + t5 + "][" + r3 + "][" + e5 + "][1]);\n                    ";
                  }) + "\n                " + Ln(e4.outChannels / 2, "", function(e5) {
                    return "\n                    x[" + e5 + "] = pack2x16float(vec2<f32>(f32(results[" + t5 + "][" + r3 + "][" + e5 + "][0]),\n                                f32(results[" + t5 + "][" + r3 + "][" + e5 + "][1])));\n                    ";
                  }) + "\n                let idx: i32 = (tileRow + OUT_PADDING + (warpY * 8) + (threadY * 2) + " + t5 + ") * (OUT_WIDTH + OUT_PADDING * 2)\n                            + (tileCol + OUT_PADDING + (warpX * 8) + (threadX * 1) + " + r3 + "); \n                outputBuffer[idx] = x;\n            }";
                });
              }) + "\n        };\n        ";
            }, e3;
          })(), Xn = (function() {
            function e3(e4, t4, r2, n2, i2) {
              void 0 === i2 && (i2 = false), this.inFeatures = e4, this.outFeatures = t4, this.kernelSize = r2, this.inPadding = n2, this.hostReadableOutput = i2, this._configuration = void 0, this._activation = void 0, this._weights = void 0, this._biases = void 0, this._inputs = void 0, this._lrImage = void 0, this._outputs = void 0, this._pipeline = void 0, this._inputShape = void 0, this._outputShape = void 0, this._sampler = void 0, this._storageFormat = void 0;
            }
            var t3 = e3.prototype;
            return t3.configure = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return t4.device.pushErrorScope("validation"), n2 = Math.floor((this.kernelSize - 1) / 2), this._outputShape = [r2.inputShape[0] - n2 + this.inPadding, r2.inputShape[1] - n2 + this.inPadding, this.outFeatures], this._storageFormat = t4.storageFormat, this._inputShape = r2.inputShape, this._configuration = r2, this._activation = r2.activation, this._outputs = new Fn(t4, this._outputShape[1], this._outputShape[0], this._storageFormat, this.hostReadableOutput ? On.DeviceToHost : On.Default), i2 = this._createShader(r2.expectedInputType), this._sampler = t4.device.createSampler({ magFilter: "linear", minFilter: "linear" }), e6.next = 1, t4.createShaderModule(i2);
                    case 1:
                      if (o2 = e6.sent, a2 = o2.module, !(s2 = o2.err) && a2) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", s2 || new Sn("Conv2d_Exit_ChromaCombine", Tn.ShaderCompilationFailed, "Failed to create shader module", true));
                    case 2:
                      return this._pipeline = t4.device.createComputePipeline({ layout: "auto", compute: { module: a2, entryPoint: "main" }, label: "conv2d_entry_upscale" }), e6.next = 3, t4.device.popErrorScope();
                    case 3:
                      (u2 = e6.sent) && new Sn("Conv2d_Exit_ChromaCombine", Tn.PipelineCreationFailed, "Failed to create compute pipeline: " + u2.message, true);
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.setWeights = function(e4) {
              this._weights = e4;
            }, t3.setBiases = function(e4) {
              this._biases = e4;
            }, t3.setInputs = function(e4) {
              this._inputs = e4;
            }, t3.setQuantOffsets = function(e4) {
            }, t3.getOutputs = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.abrupt("return", this._outputs);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.setLowResTexture = function(e4) {
              this._lrImage = e4;
            }, t3.close = function() {
              var e4;
              this._weights.close(), null == (e4 = this._biases) || e4.close(), this._outputs.close();
            }, t3.createFinalizedPass = function(e4, t4, r2) {
              if (!(this._inputs instanceof Nn)) return { err: new Sn("Conv2d_Exit_ChromaCombine", Tn.BadParameters, "ModelLayerInput is not a Buffer", true) };
              try {
                var n2, i2 = { label: "conv2d_exit" };
                if (this._lrImage instanceof GPUTexture) n2 = this._lrImage.createView();
                else if (this._lrImage instanceof VideoFrame) n2 = e4.device.importExternalTexture({ source: this._lrImage });
                else {
                  if (!(this._lrImage instanceof GPUExternalTexture)) return { err: new Sn("Conv2d_Exit_ChromaCombine", Tn.BadParameters, "Input is not one of GPUTexture, VideoFrame, GPUExternalTexture", true) };
                  n2 = this._lrImage;
                }
                var o2 = this._pipeline.getBindGroupLayout(0), a2 = [{ binding: 0, resource: this._outputs.deviceTexture.createView() }, { binding: 1, resource: { buffer: this._inputs.deviceBuffer } }, { binding: 2, resource: { buffer: this._weights.deviceBuffer } }, { binding: 3, resource: n2 }, { binding: 4, resource: this._sampler }];
                this._biases && a2.push({ binding: 3, resource: { buffer: this._biases.deviceBuffer } });
                var s2 = e4.device.createBindGroup({ layout: o2, entries: a2 });
                r2 && (i2.timestampWrites = { querySet: r2, beginningOfPassWriteIndex: 0, endOfPassWriteIndex: 1 });
                var u2 = t4.beginComputePass(i2);
                return u2.setPipeline(this._pipeline), u2.setBindGroup(0, s2), u2.dispatchWorkgroups(Math.ceil(this._outputShape[1] / 16), Math.ceil(this._outputShape[0] / 16), 1), u2.end(), { pass: u2 };
              } catch (e5) {
                return { err: new Sn("Conv2d_Exit_ChromaCombine", Tn.InferenceFailed, "Failed to create finalized pass: " + ((null == e5 ? void 0 : e5.message) || "unknown"), true) };
              }
            }, t3._createShader = function(e4) {
              var t4 = e4 === Rn.External ? "texture_external" : "texture_2d<fT>", r2 = this._configuration.dataType;
              this._activation && this._activation.getWGSL("sum", r2);
              var n2 = "";
              return this._biases && (n2 = "@group(0) @binding(5) var<storage, read> biases: array<fT, M * N>;"), "\n            " + (this._configuration.dataType === Un.F16 ? "enable f16;" : "") + "\n\n            const OUT_FEATURES: u32 = " + this.outFeatures + ";\n            const OUT_WIDTH: u32 = " + this._outputShape[1] + ";\n            const OUT_HEIGHT: u32 = " + this._outputShape[0] + ";\n            const IN_WIDTH: u32 = " + this._inputShape[1] + ";\n            const IN_HEIGHT: u32 = " + this._inputShape[0] + ";\n            const IN_FEATURES: u32 = " + this.inFeatures + ";\n            const IN_PADDING: u32 = " + this.inPadding + ";\n            const KERNEL_SIZE: u32 = " + this.kernelSize + ";\n            const GROUP_SIDE: u32 = 16;\n            const GROUP_THREADS: u32 = GROUP_SIDE * GROUP_SIDE;\n\n            alias fT = " + this._configuration.dataType + ";\n            @group(0) @binding(0) var outputTexture: texture_storage_2d<" + this._storageFormat + ", write>;\n            @group(0) @binding(1) var<storage, read> inputBuffer: array<fT, (IN_HEIGHT + IN_PADDING * 2) * (IN_WIDTH + IN_PADDING * 2) * IN_FEATURES>;\n            @group(0) @binding(2) var<storage, read> weightsBuffer: array<fT, OUT_FEATURES * IN_FEATURES * KERNEL_SIZE * KERNEL_SIZE>;\n            @group(0) @binding(3) var lrTexture: " + t4 + ";\n            @group(0) @binding(4) var inputSampler: sampler;\n            " + n2 + "\n            const Y_COEF = vec3<fT>(0.299, 0.587, 0.114);\n            fn rgbToYuv(x: vec3<f32>) -> vec3<f32> {\n                let y = dot(x, vec3<f32>(Y_COEF));\n                return vec3<f32>(\n                    y, 0.493*(x.b-y), 0.877*(x.r-y)\n                );\n            }\n\n            fn yuvToRgb(x: vec3<f32>) -> vec3<f32> {\n                return vec3<f32>(\n                    x.x + 1.0 / 0.877 * x.z,\n                    x.x - 0.39393 * x.y - 0.58081 * x.z,\n                    x.x + 1.0 / 0.493 * x.y\n                );\n            }\n\n            @compute @workgroup_size(GROUP_THREADS,1,1)\n            fn main(@builtin(global_invocation_id) global_id: vec3<u32>,\n                    @builtin(local_invocation_index) tid: u32,\n                    @builtin(workgroup_id) group_id: vec3<u32>) {\n                let cRow = group_id.y * GROUP_SIDE + tid / GROUP_SIDE;\n                let cCol = group_id.x * GROUP_SIDE + (tid % GROUP_SIDE);\n\n                // TODO: Optimize. First, load these into workgroup memory.\n                // assuming symmetric odd-numbered kernel size\n                var inputs: array<fT, KERNEL_SIZE * KERNEL_SIZE * IN_FEATURES>;\n                let off: u32 = (KERNEL_SIZE-1)/2;\n\n                for (var m: u32 = 0; m < KERNEL_SIZE; m++) {\n                    for (var n: u32 = 0; n < KERNEL_SIZE; n++) {\n                        for (var c: u32 = 0; c < IN_FEATURES; c++) {\n                            let in_idx = (cRow + n - off + IN_PADDING) * (IN_WIDTH + IN_PADDING * 2) * IN_FEATURES + (cCol + m - off + IN_PADDING) * IN_FEATURES + c;\n                            inputs[n * KERNEL_SIZE * IN_FEATURES + m * IN_FEATURES + c] = inputBuffer[in_idx];\n                        }\n                    }\n                }\n\n                // Should probably change the order of these loops to better map to channel ordering\n                // Also move weights loading into workgroup memory...\n                var color = vec4<f32>(0.0, 0.0, 0.0, 1.0);\n                for (var z: u32 = 0; z < OUT_FEATURES; z++) {\n                    var sum: fT = 0.0;\n                    for (var j: u32 = 0; j < IN_FEATURES; j++) {\n                        var weights: array<fT, KERNEL_SIZE * KERNEL_SIZE>;\n                        for (var m: u32 = 0; m < KERNEL_SIZE; m++) {\n                            for (var n: u32 = 0; n < KERNEL_SIZE; n++) {\n                                var weights_off = z * IN_FEATURES * KERNEL_SIZE * KERNEL_SIZE + m * KERNEL_SIZE * IN_FEATURES + n * IN_FEATURES + j;\n                                weights[m * KERNEL_SIZE + n] = weightsBuffer[weights_off];\n                            }\n                        }\n                        {\n                            {\n                                sum += inputs[0 * KERNEL_SIZE * IN_FEATURES + 0 * IN_FEATURES + j] * weights[0 * KERNEL_SIZE + 0];\n                                sum += inputs[0 * KERNEL_SIZE * IN_FEATURES + 1 * IN_FEATURES + j] * weights[0 * KERNEL_SIZE + 1];\n                                sum += inputs[0 * KERNEL_SIZE * IN_FEATURES + 2 * IN_FEATURES + j] * weights[0 * KERNEL_SIZE + 2];\n                            }\n                            {\n                                sum += inputs[1 * KERNEL_SIZE * IN_FEATURES + 0 * IN_FEATURES + j] * weights[1 * KERNEL_SIZE + 0];\n                                sum += inputs[1 * KERNEL_SIZE * IN_FEATURES + 1 * IN_FEATURES + j] * weights[1 * KERNEL_SIZE + 1];\n                                sum += inputs[1 * KERNEL_SIZE * IN_FEATURES + 2 * IN_FEATURES + j] * weights[1 * KERNEL_SIZE + 2];\n                            }\n                            {\n                                sum += inputs[2 * KERNEL_SIZE * IN_FEATURES + 0 * IN_FEATURES + j] * weights[2 * KERNEL_SIZE + 0];\n                                sum += inputs[2 * KERNEL_SIZE * IN_FEATURES + 1 * IN_FEATURES + j] * weights[2 * KERNEL_SIZE + 1];\n                                sum += inputs[2 * KERNEL_SIZE * IN_FEATURES + 2 * IN_FEATURES + j] * weights[2 * KERNEL_SIZE + 2];\n                            }\n                        }\n                    }\n                    color[z] = clamp(f32(sum), 0.0627, 1.0-0.0627);\n                }\n\n                let texSize = vec2<f32>(f32(OUT_WIDTH), f32(OUT_HEIGHT));\n                let texelSize = 1.0 / texSize;\n                let inPos = vec2<f32>(f32(cCol+1), f32(cRow)) * texelSize;\n                let lr = rgbToYuv(textureSampleBaseClampToEdge(lrTexture, inputSampler, inPos).rgb * 0.8627 + 0.0627);\n                let mixval: f32 = f32(cCol > 3) * f32(cRow > 3) * f32(cCol < OUT_WIDTH - 3) * f32(cRow < OUT_HEIGHT - 3);\n                color.x = mix(lr.x, color.x, mixval);\n                color = vec4((yuvToRgb(vec3<f32>(color.x, lr.y, lr.z)) * 1.1591) - 0.0627, 1.0);\n                textureStore(outputTexture, vec2<u32>(cCol, cRow), color);\n            };\n        ";
            }, e3;
          })(), Jn = (function() {
            function e3(e4) {
              this.a = e4;
            }
            var t3 = e3.prototype;
            return t3.activationsPerFeature = function() {
              return 1;
            }, t3.getWGSL = function(e4) {
              return this.a + " * " + e4 + " + (1 - " + this.a + ") * max(0, " + e4 + ")";
            }, e3;
          })();
          function $n(e3, t3) {
            var r2 = "undefined" != typeof Symbol && e3[Symbol.iterator] || e3["@@iterator"];
            if (r2) return (r2 = r2.call(e3)).next.bind(r2);
            if (Array.isArray(e3) || (r2 = (function(e4, t4) {
              if (e4) {
                if ("string" == typeof e4) return ei(e4, t4);
                var r3 = {}.toString.call(e4).slice(8, -1);
                return "Object" === r3 && e4.constructor && (r3 = e4.constructor.name), "Map" === r3 || "Set" === r3 ? Array.from(e4) : "Arguments" === r3 || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(r3) ? ei(e4, t4) : void 0;
              }
            })(e3)) || t3 && e3 && "number" == typeof e3.length) {
              r2 && (e3 = r2);
              var n2 = 0;
              return function() {
                return n2 >= e3.length ? { done: true } : { done: false, value: e3[n2++] };
              };
            }
            throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
          }
          function ei(e3, t3) {
            (null == t3 || t3 > e3.length) && (t3 = e3.length);
            for (var r2 = 0, n2 = Array(t3); r2 < t3; r2++) n2[r2] = e3[r2];
            return n2;
          }
          var ti = (function() {
            function e3(e4, t4, r2) {
              this.config = e4, this.gpuContext = t4, this.onError = r2, this.entry = void 0, this.exit = void 0, this.hidden = [], this.timingInfoEntry = void 0, this.timingInfoExit = void 0, this.timingQueryEntry = void 0, this.timingQueryExit = void 0, this.weightsMap = void 0, this.weightsBinary = void 0, this.width = 0, this.height = 0, this.initialized = false, this.initializing = false, this.MIN_AREA = 0, this.MAX_AREA = 0, this.SCALE_FACTOR = 0, this.models = {}, Be.info("[ModelIVSEN]: Config", this.config);
            }
            var t3 = e3.prototype;
            return t3.capabilities = function() {
              return Object.values(this.models).map(function(e4) {
                var t4 = e4.config;
                return { id: t4.id, heightFrom: t4.height, widthFrom: t4.width, heightTo: t4.height * t4.scaleFactor, widthTo: t4.width * t4.scaleFactor, framerateFrom: t4.framerate, scoreModifier: t4.scoreModifier, encoder: t4.encoder, codec: t4.codec };
              });
            }, t3.addModel = function(e4, t4) {
              Be.log("Added " + e4.id + " model", e4, t4), this.models[e4.id] = { config: e4, params: t4 };
            }, t3.trySetActiveModel = function(e4) {
              var t4 = this.models[e4];
              return !!t4 && (this.MIN_AREA = t4.config.width * t4.config.height, this.MAX_AREA = this.MIN_AREA, this.SCALE_FACTOR = t4.config.scaleFactor, this.weightsMap = t4.params.weightsMap, this.weightsBinary = t4.params.weightsBinary, true);
            }, t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return Be.info("[ModelIVSEN]: Initializing"), this.initialized = false, this.initializing = false, this.entry = void 0, this.exit = void 0, this.width = 0, this.height = 0, Be.info("[ModelIVSEN]: Init complete", this.models), e6.abrupt("return", void 0);
                    case 1:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.infer = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2, c2, l2, d2, f2, h2, p2, v2, g2, m2, y2, b2, E2, S2, T2, _2, C2, k2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.initializing) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", void 0);
                    case 1:
                      if (this.trySetActiveModel(r2.pipelineId)) {
                        e6.next = 2;
                        break;
                      }
                      return e6.abrupt("return", void 0);
                    case 2:
                      if (n2 = t4 instanceof VideoFrame ? t4.displayWidth : t4.width, i2 = t4 instanceof VideoFrame ? t4.displayHeight : t4.height, !((o2 = n2 * i2) < this.MIN_AREA || o2 > this.MAX_AREA)) {
                        e6.next = 3;
                        break;
                      }
                      return this.initialized = false, this.initializing = false, e6.abrupt("return", void 0);
                    case 3:
                      if (n2 === this.width && i2 === this.height || (this.initialized = false), this.initialized) {
                        e6.next = 4;
                        break;
                      }
                      return this.reinit(n2, i2), e6.abrupt("return", void 0);
                    case 4:
                      for (this.gpuContext.device.pushErrorScope("validation"), a2 = this.gpuContext.device.createCommandEncoder(), this.entry && (this.entry.setInputs(t4), this.entry.createFinalizedPass(this.gpuContext, a2, this.timingQueryEntry), this.timingInfoEntry && this.timingQueryEntry && a2.resolveQuerySet(this.timingQueryEntry, 0, 2, this.timingInfoEntry.deviceBuffer, 0)), s2 = $n(this.hidden); !(u2 = s2()).done; ) (c2 = u2.value).layer && c2.layer.createFinalizedPass(this.gpuContext, a2, c2.timingQuery), c2.timingQuery && c2.timingInfo && a2.resolveQuerySet(c2.timingQuery, 0, 2, c2.timingInfo.deviceBuffer, 0);
                      return this.exit && (this.exit.setLowResTexture(t4), this.exit.createFinalizedPass(this.gpuContext, a2, this.timingQueryExit), this.timingInfoExit && this.timingQueryExit && a2.resolveQuerySet(this.timingQueryExit, 0, 2, this.timingInfoExit.deviceBuffer, 0)), this.gpuContext.queue.submit([a2.finish()]), e6.prev = 5, e6.next = 6, this.gpuContext.queue.onSubmittedWorkDone();
                    case 6:
                      e6.next = 8;
                      break;
                    case 7:
                      return e6.prev = 7, C2 = e6.catch(5), this.handleError(new Sn("ModelIVSEN", Tn.InferenceFailed, null != (l2 = null == C2 ? void 0 : C2.message) ? l2 : "Unknown error", true)), e6.abrupt("return", void 0);
                    case 8:
                      if (!this.timingInfoEntry) {
                        e6.next = 11;
                        break;
                      }
                      return e6.next = 9, this.timingInfoEntry.copyToHost(this.gpuContext.device);
                    case 9:
                      if (d2 = e6.sent, f2 = d2.err, h2 = d2.buffer, !f2) {
                        e6.next = 10;
                        break;
                      }
                      return this.handleError(f2), e6.abrupt("return");
                    case 10:
                      Be.debug("[ModelIVSEN]: entry timing=" + ri(h2));
                    case 11:
                      p2 = $n(this.hidden);
                    case 12:
                      if ((v2 = p2()).done) {
                        e6.next = 16;
                        break;
                      }
                      if (!(g2 = v2.value).timingInfo) {
                        e6.next = 15;
                        break;
                      }
                      return e6.next = 13, g2.timingInfo.copyToHost(this.gpuContext.device);
                    case 13:
                      if (m2 = e6.sent, y2 = m2.err, b2 = m2.buffer, !y2) {
                        e6.next = 14;
                        break;
                      }
                      return this.handleError(y2), e6.abrupt("return");
                    case 14:
                      Be.debug("[ModelIVSEN]: hidden[" + g2.idx + "] timing=" + ri(b2));
                    case 15:
                      e6.next = 12;
                      break;
                    case 16:
                      if (!this.timingInfoExit) {
                        e6.next = 19;
                        break;
                      }
                      return e6.next = 17, this.timingInfoExit.copyToHost(this.gpuContext.device);
                    case 17:
                      if (E2 = e6.sent, S2 = E2.err, T2 = E2.buffer, !S2) {
                        e6.next = 18;
                        break;
                      }
                      return this.handleError(S2), e6.abrupt("return");
                    case 18:
                      Be.debug("[ModelIVSEN]: exit timing=" + ri(T2));
                    case 19:
                      if (this.gpuContext.device.popErrorScope().then(function(e7) {
                        var t5;
                        e7 && k2.handleError(new Sn("ModelIVSEN", Tn.InferenceFailed, null != (t5 = null == e7 ? void 0 : e7.message) ? t5 : "Unknown error", true));
                      }).catch(function(e7) {
                        var t5;
                        k2.handleError(new Sn("ModelIVSEN", Tn.InferenceFailed, null != (t5 = null == e7 ? void 0 : e7.message) ? t5 : "Unknown error", true));
                      }), this.exit) {
                        e6.next = 20;
                        break;
                      }
                      return Be.warn(["ModelIVSEN: Exit layer does not exist, cant return texture"]), e6.abrupt("return", void 0);
                    case 20:
                      return e6.next = 21, this.exit.getOutputs();
                    case 21:
                      if ((_2 = e6.sent) instanceof Fn) {
                        e6.next = 22;
                        break;
                      }
                      return this.initialized = false, Be.warn(["ModelIVSEN: Exit layer did not return a texture"]), e6.abrupt("return", void 0);
                    case 22:
                      return e6.abrupt("return", _2.deviceTexture);
                    case 23:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[5, 7]]);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.reinit = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2) {
                var n2, i2, o2, a2, s2, u2, c2, l2, d2, f2, h2, p2, v2, g2, m2, y2, b2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (n2 = this.weightsBinary, i2 = this.weightsMap, n2 && i2) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", void 0);
                    case 1:
                      return this.initializing = true, this.cleanup(), e6.next = 2, this.configureEntrypoint(t4, r2, this.SCALE_FACTOR, 1, 16, n2, i2);
                    case 2:
                      if (o2 = e6.sent, a2 = o2.err, s2 = o2.entry, !a2 && s2) {
                        e6.next = 3;
                        break;
                      }
                      return this.handleError(a2 || new Sn("Model", Tn.PipelineCreationFailed, "Failed to create entry layer", true)), e6.abrupt("return");
                    case 3:
                      this.entry = s2, this.hidden = [], u2 = 0;
                    case 4:
                      if (!(u2 < 4)) {
                        e6.next = 12;
                        break;
                      }
                      if (c2 = void 0, l2 = 8, 0 !== u2) {
                        e6.next = 6;
                        break;
                      }
                      return e6.next = 5, this.entry.getOutputs();
                    case 5:
                      c2 = e6.sent, l2 = 16, e6.next = 8;
                      break;
                    case 6:
                      return e6.next = 7, this.hidden[u2 - 1].layer.getOutputs();
                    case 7:
                      c2 = e6.sent;
                    case 8:
                      return e6.next = 9, this.configureMid(Math.ceil(t4 * this.SCALE_FACTOR), Math.ceil(r2 * this.SCALE_FACTOR), l2, 8, n2, i2, c2, 2 * u2);
                    case 9:
                      if (d2 = e6.sent, f2 = d2.err, h2 = d2.layer) {
                        e6.next = 10;
                        break;
                      }
                      return this.handleError(f2 || new Sn("Model", Tn.PipelineCreationFailed, "Failed to create midlayer", true)), e6.abrupt("return");
                    case 10:
                      this.hidden.push({ layer: h2, timingInfo: null, timingQuery: null, idx: u2 });
                    case 11:
                      u2++, e6.next = 4;
                      break;
                    case 12:
                      return e6.next = 13, this.configureExit(Math.ceil(t4 * this.SCALE_FACTOR), Math.ceil(r2 * this.SCALE_FACTOR), 8, 1, n2, i2);
                    case 13:
                      if (p2 = e6.sent, v2 = p2.err, g2 = p2.exit, !v2 && g2) {
                        e6.next = 14;
                        break;
                      }
                      return this.handleError(v2 || new Sn("Model", Tn.PipelineCreationFailed, "Failed to create exit layer", true)), e6.abrupt("return");
                    case 14:
                      if (this.exit = g2, this.gpuContext.timingInfoEnabled) {
                        for (this.timingInfoEntry = new Nn(this.gpuContext, 16, On.TimingQuery), m2 = $n(this.hidden); !(y2 = m2()).done; ) (b2 = y2.value).timingInfo = new Nn(this.gpuContext, 16, On.TimingQuery), b2.timingQuery = this.gpuContext.device.createQuerySet({ type: "timestamp", count: 2 });
                        this.timingInfoExit = new Nn(this.gpuContext, 16, On.TimingQuery), this.timingQueryEntry = this.gpuContext.device.createQuerySet({ type: "timestamp", count: 2 }), this.timingQueryExit = this.gpuContext.device.createQuerySet({ type: "timestamp", count: 2 });
                      }
                      this.width = t4, this.height = r2, this.initializing = false, this.initialized = true;
                    case 15:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2) {
                return e4.apply(this, arguments);
              };
            })(), t3.cleanup = function() {
              var e4, t4, r2, n2;
              null == (e4 = this.exit) || e4.close();
              for (var i2, o2 = $n(this.hidden); !(i2 = o2()).done; ) i2.value.layer.close();
              null == (t4 = this.entry) || t4.close(), null == (r2 = this.timingInfoEntry) || r2.close(), null == (n2 = this.timingInfoExit) || n2.close();
            }, t3.configureEntrypoint = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2, i2, o2, a2, s2) {
                var u2, c2, l2, d2, f2, h2, p2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return u2 = new Vn(i2, o2, 3, n2, 1, true, false), c2 = s2["init_feat.0"].weight, l2 = c2.bits / 8, d2 = a2.slice(c2.offset, c2.offset + c2.count * l2), f2 = { inputShape: [r2, t4, i2], weightShape: [3, 3, o2 * i2], bias: false, dataType: 4 === l2 ? Un.F32 : Un.F16, expectedInputType: Rn.External, activation: new Jn(0.1), loadWidthBytes: this.config.loadWidthBytes }, h2 = new Nn(this.gpuContext, 9 * i2 * o2 * l2, On.HostToDevice), e6.next = 1, h2.copyToDevice(this.gpuContext, d2);
                    case 1:
                      return u2.setWeights(h2), e6.next = 2, u2.configure(this.gpuContext, f2);
                    case 2:
                      if (!(p2 = e6.sent)) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", { err: p2 });
                    case 3:
                      return e6.abrupt("return", { entry: u2 });
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2, i2, o2, a2, s2) {
                return e4.apply(this, arguments);
              };
            })(), t3.configureMid = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2, i2, o2, a2, s2, u2) {
                var c2, l2, d2, f2, h2, p2, v2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return c2 = a2["bottleneck." + u2].weight, l2 = c2.bits / 8, d2 = o2.slice(c2.offset, c2.offset + c2.count * l2), f2 = 4 === l2 ? new Bn(n2, i2, false) : new Zn(n2, i2, false), h2 = { inputShape: [r2, t4, n2], weightShape: [3, 3, i2 * n2], bias: false, dataType: 4 === l2 ? Un.F32 : Un.F16, expectedInputType: Rn.Buffer, activation: new Jn(0.1), loadWidthBytes: this.config.loadWidthBytes }, p2 = new Nn(this.gpuContext, 9 * n2 * i2 * l2, On.HostToDevice), e6.next = 1, p2.copyToDevice(this.gpuContext, d2);
                    case 1:
                      return f2.setInputs(s2), f2.setWeights(p2), e6.next = 2, f2.configure(this.gpuContext, h2);
                    case 2:
                      if (!(v2 = e6.sent)) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", { err: v2 });
                    case 3:
                      return e6.abrupt("return", { layer: f2 });
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2, i2, o2, a2, s2, u2) {
                return e4.apply(this, arguments);
              };
            })(), t3.configureExit = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r2, n2, i2, o2, a2) {
                var s2, u2, c2, l2, d2, f2, h2, p2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return s2 = new Xn(n2, i2, 3, 1, false), u2 = a2.reconstruct.weight, c2 = u2.bits / 8, l2 = o2.slice(u2.offset, u2.offset + u2.count * c2), d2 = { inputShape: [r2, t4, n2], weightShape: [3, 3, i2 * n2], bias: false, dataType: 4 === c2 ? Un.F32 : Un.F16, expectedInputType: Rn.External, activation: new Jn(0.1), loadWidthBytes: this.config.loadWidthBytes }, f2 = new Nn(this.gpuContext, 9 * n2 * i2 * c2, On.HostToDevice), e6.next = 1, f2.copyToDevice(this.gpuContext, l2);
                    case 1:
                      return this.hidden[this.hidden.length - 1].layer.getOutputs().then(function(e7) {
                        return s2.setInputs(e7);
                      }).catch(function(e7) {
                        var t5;
                        p2.handleError(new Sn("Model", Tn.BadParameters, null != (t5 = null == e7 ? void 0 : e7.message) ? t5 : "Failed to access hidden layer", true));
                      }), s2.setWeights(f2), e6.next = 2, s2.configure(this.gpuContext, d2);
                    case 2:
                      if (!(h2 = e6.sent)) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", { err: h2 });
                    case 3:
                      return e6.abrupt("return", { exit: s2 });
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function(t4, r2, n2, i2, o2, a2) {
                return e4.apply(this, arguments);
              };
            })(), t3.handleError = function(e4) {
              this.initializing = false, this.initialized = false, this.onError(e4);
            }, e3;
          })(), ri = function(e3) {
            if (!e3) return Be.warn("[ModelIVSEN]: timingInfo buffer is undefined"), 0;
            var t3 = 0;
            try {
              var r2 = new BigInt64Array(e3);
              t3 = Number(r2[1] - r2[0]) / 1e9;
            } catch (e4) {
              Be.warn("[ModelIVSEN': Failed to get seconds from timingInfo");
            }
            return t3;
          }, ni = { "init_feat.0": { weight: { offset: 0, bits: 16, count: 144 } }, "bottleneck.0": { weight: { offset: 288, bits: 16, count: 1152 } }, "bottleneck.2": { weight: { offset: 2592, bits: 16, count: 576 } }, "bottleneck.4": { weight: { offset: 3744, bits: 16, count: 576 } }, "bottleneck.6": { weight: { offset: 4896, bits: 16, count: 576 } }, reconstruct: { weight: { offset: 6048, bits: 16, count: 72 } } }, ii = (function(e3) {
            function t3(t4, r3, n2) {
              var i2;
              return (i2 = e3.call(this, "SuperResolutionTransformer", n2) || this).config = t4, i2.gpuContext = r3, i2.listeners = n2, i2.model = void 0, i2.model = new ti(t4, r3, i2.listeners.error), i2;
            }
            Lr()(t3, e3);
            var r2 = t3.prototype;
            return r2.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4, r3, n2, i2, o2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (t4 = this.loadLocalModelParams(), r3 = t4.result, !(n2 = t4.err)) {
                        e6.next = 1;
                        break;
                      }
                      return e6.abrupt("return", n2);
                    case 1:
                      return e6.next = 2, this.model.init();
                    case 2:
                      if (!(i2 = e6.sent)) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", i2);
                    case 3:
                      this.config.models.forEach(function(e7) {
                        "local" === e7.path && r3 && o2.model.addModel(e7, r3);
                      }), this.listeners.capabilities(this.capabilties());
                    case 4:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), r2.transformFn = (function() {
              var e4 = Ne()(Ue().mark(function e5(t4, r3) {
                var n2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return e6.prev = 0, e6.next = 1, this.model.infer(t4, r3);
                    case 1:
                      return e6.abrupt("return", e6.sent);
                    case 2:
                      return e6.prev = 2, n2 = e6.catch(0), this.listeners.error(new Sn("SuperResolutionTransformer", Tn.InferenceFailed, "Unhandled exception " + (null == n2 ? void 0 : n2.message), true)), e6.abrupt("return", void 0);
                    case 3:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[0, 2]]);
              }));
              return function(t4, r3) {
                return e4.apply(this, arguments);
              };
            })(), r2.loadLocalModelParams = function() {
              var e4 = (function() {
                try {
                  for (var e5 = atob("SKM6JcKhrbrmQFkvoDrqwEewEbtDQuM5fjvbw7C5eLDFO1crj7gZQkkzGTp0wwO0nLAOqdqsKi6aufS2Db7bQTGyWKAQsQ4wNrqdQV+4abBixRhBajkrOKS5V6+aLDIfyUDLwOGtdq4qL/mkAbK7L5YzyzurvaW9cjicOEwzJKLuOx8z6DzvwJI6LjP3u3swZrjXNE2xjMALP7K0VixKts6gVLWVOTnBFzSzOY48OCgMtwa8JLkwM2IkLCqlQIg9WLmqPEmoRD68wlxAdrwOQVG/kbh+MlAsTqlXOhIxcEBmwk26ILn/OAcyHqysLNaqYzL0w001gbHKQy21FyoYOqG6nK2Dw4JDn6kPM5eyjLR1MW2tX7juw+o/YjWBPx24gcG3sXm+wbRJPCa2DEAJugO8uiY4M70xZzIGtXM8jLQyRmK4erh0OHarij0YvwOz+r54srOvFreoLRkwKUBNPRS+ozM6Nma45bShvZi6rbQXvkOwHriTuK88wrVrtEotnLQBQKnANzSaxL/BBb0LumayPqBmPFm8cSo2vDE94r+7OOjAGr60w+TC0bk4vGWncke9Oay8L7XaOF5CbrK5wZS0L7O4PWG0zrlKOPi25D++xzk5IbntqAK4QbgVOgy5ejyeu3o6VDQdN3U+0rQKuFa+0sHoOzE3dbh7vqk65sQkwGE+iroFLi8xJrwCO405U0iGPMe/4D1SuDO1GTzSPAk3LbM7uqO47DzaPSU8Irt2xys8OD1cOqS9jTfJuJbA6Tsju0yvQLVQPLE1cjaSsoQot7tbOh01hbdzvk4wvsBeQhY/csByQM+z4754vE6/jkASvRm/jLsWtbw95zygvAm84Ll6PI62bkBHPb2yJ778PeK84jv9ucq72r/KuFW1KDlMt3o7nsEPs2SwFbv8PJlAXD/EplQ9KTvcLfg6MChsPWawwsJUPUY0pTTwufI+8bhKOpjALiJ2Or48wL4etZJAEsC/Ql8uv75iw6LCbT01xaM7S75fvDg4FMGAuOooIrY4upyhWrzzPAoyNrNeOB44sEGLPNe4OrdduwutaD/WPdU8jzAVQNm/ib1vN3e8Hr8xwaI31L9KPdU4n8S9uzCzC7hGtuO9MsNFPqM7/bZeyS5Baj0XuoXEQLZJvb6+k7xmNXqsrrATphwtMLyPOuBAzT2gtWU1HrSoN/M41LnvRCo8RsKespQ+G8EKQEs5VriPPou74TrkPBvCMrwytQa9lbvTOCI9ijlPITrANzAXP3S4djYTK8I8ILzeuAytNrzoMsc7XLlBOdQ8NjX0reM42T9AwSs9173bunrBXj38xDa/Vjy2wDXCAUBqvzu+1cJsxPXAo8NxNns/lyrjQOhAJjGeK3q5sL+Dulo+EjyVOMk/kLQ/OqmxtaiQQHC6cC1nvRU7E0EbvIa9m7HyuKa8fUJ7r2+8ij92Oow77LMsN9A/YCxWPGxAeTV3NA43F7jbvJ+9HT5UwDs/NT4GvvU6NDWrubS4kDsCO8w0E7pOPuS4cTtxNR6z1azesmy8Y0EJOvy9mrQSOOAzZa3uNY4v2Dl9NzY3RzwbsLG/gbh9vY0/Ar4sOQI6yL3hNB6527wPtSSxAbUbObU6GL23OWA+ELoxuLe6qjR2upK72Dt6wgc9UbJaOMg65jdktJwzwigHuYi0tjuyvDI9z71dMIK8iTeuOTIzbb9AOxw6DTgFvMW8nTkxuCi7iT02JEy/ZEQZt1G8OS2ls449aj77vC08GKQJOJ28HjI6skI1PDe5uGA5tzCIsOm7uLv5sbu4cjVpp+8xxLn+t8ewUz2IMsgwI7NlMxY0mTvYqEE4LLGxvBW6+sOqPfLB9DYJuZempz4bMnm+XbexJKM3Xrr6Pb85K7c0sgypxrkquj0x9TVYyTc9YjslPga7HTW5uFExWUB3OSK5IbgcOEyvEj2QODIz0TlMHjc1ObzDu4Syva5kQnSwxrT2NQ27Qzt0uUGp6zZEM78noK3HOVk8VjwzOLK4zKqgPtw7cLxVxIkuTrvBuE++5bhDu8g/iDIDLvdCGj5OuQc9orw7u/g87cGKvNKmG7w2uiy8h8I9OGw6OToivZS4msCbv1PCwb2TvdLBxyuTuq02DbE1QG02Drzyv5BDLDjNuthAt0MLOPM9RryiwADA2LjsvSK4ortBwFTBhbm2u9a9ETzxsygn0bEUuRM198EFHIW1zrgCvIW3HkBzMMXAozzOsNO1zTbkL7pBvbsoP545Uz8cPT68BD/4t4e5yj11OYUwGDlWN7Uv9rWivqu5BDZ4wFy9CsMUweA/j7musnU5ZrTTuVs8Cz6FvEO1FjxSsPQ5ccAARKZAhsRmQhc59rz8wSCwuESvw5g+rUMIPQO1pUCStRHFWbisuLq4uTdDOe+tzrQwpr2877U8wDo9sbDPPKIyBLmjrN024Dx/P7K+xr+wtjW9l7tdPo5BAT8uM0A0dz/usKC/OcHDvHY2PTomQJ08QMFXpkS4PMDiwG4xlMAiPP6+1TTzPvnAK73Ev5+/VkIMyLM3XjtawzU6ezTsPvbA6zwMwDoqNDfVuhW12DO6Nag57jAvrCm4wzmNOPa9T6h2tPu86L/eN184dMWRQGu4vzwiPXy+Lbv0wc6uA7Xiuh2++7zOQPuu6L4Bveg22Lp+Nec4sb3pQWbB1rt+sUTCUb0xO+RC5DRHuLw8ZqZSu7a5NzWZtBG7TDefuqSz571tuPUokT5ftBE4HrxZNnk3j7uaNTI4D0G9uia++iduOuPC5b1BqljBwLsvOoK3VD1ivgQ5aL8gwGq1vzyHtuI8ZL6iugS8UbQaO3zDmsV2uDQspL5/PnVGdsEetym67zdJwlm65z7/sK0oO8MvvO69X8LsPJ+6LsZRP5s5GrwNRF++X8cFNFy5TrVpt9O05LL9sra9Ej71t2I9QjT3NP0/OL42uR+6rbAdvZ1B4DzNPCo+77kgunG5RUGOOEWkGr8KNZu+JK7xtMm9EDhRPprCD0BDMiy10rRPtgO39De3vqooTLy3P445cLwIPY8z58NgOJo/QsDpuse3UzefOLa9lbrtNfYx8r4TwBC12z8VQZWwNz8QuJa6qsFZs5e8s8DLNzO/MLtNQqDB5j7GtuO7VT1Dv5Q9B8JGuuI2O7pouOa5niIftxI7mLJgvQm45Tu8Pv+srTijQa0y9SylJjQ9qDUINSG40rS7OXy4Hr5QPFW4+7x5PY88KUFCNGZCHTR/PmZBvb9ItQM9l8H9vKOzY8J7PYLB2UDbNCs/Hr05QQlANj2jtZS9Vz4wvWw86zz3Ob8uq8FtssW92jOJubq/7iXirvs6Tb4ZOQu4/sBxwDY52L7sPwdEWjkcv1+2WrkpPAi9sTRVNL88K7+gwgQ5QDXZMmI5y735QMs36zvluIu7fr18uL+xSi0RutRCC7QCxIQ0WzQQvka/mb7KOwgyDi39O7Y1/jBlrT4ptzVbKmUuoK6FtUg3tS9FOoU6Hy3XNTUulbcjNoUxb7R9pfMpRjHAvG8iFrjksDsqkzVcNnQ2OS+8r465L7rxtwK4GjniM2k/frxYOeO5iD1ysIcwkKT/vFO1Vbt7uC4oRDWRKM207EAfs1K5WrWSvOU1UbeoOMFAzLJeNEG7o7Wdu+m51rqpP5K38DP8Nmk14Da+uOYwFTiOvIW7Pr+6vxS+QLvnLCBCxLu0tDy1ojgQuJy+iqpquu261i1ROLOxTTnmLcE4FbReOMkxZjJ5PaC1vLY3sQZBhjNTK+k5UrTptxe+5byLQYQ09jWxsi43zzR2sWwyvbhhMzYyRDibMfY2RSm/NRQ4NDDStvcq07ZsNCsvNS2kwAOqOrCGKT4jvBdwnDadiLKRL3AufKXuNbAuHbJ4sMUnta0TM5AzzLSANTG0nbvROny7qK+ZKCGxgioiM/cwqzcJLqmyiyoEwEouWbDqKy88JqQcunC8PkRvuwA9Lbj4PZu45LG+LzyxgS2Tqhakia8SLmuwTqFqLdO0nLFItCCwZapfIewij7EKwJy+R8AgSIMybLVJK8MyyK6+LE6vrrQ0MtmzSDhGO1EwSy6PMCY6hrOrr8M5hbncKhiv0Da/vpw5+bH3OEe7wieEupWx30DVsw+wTrhmQLa6+LTvMxc6KCyAtE43LbgNs+uwhjhwwUY4S7hRufsvEzPyN2A1l0KNsCqveL9XLjq07rGcMSVFnrvrrlU7nb3YsPI2bDlxwYs0tCmrN6k0kaTJKUapHrR0trE2B71RsmKyaLNzIpQu5SOgtZ40+TH+sSwcca8YOAEtdzbjqRAzOLPgtGmh273AtlY5I8HAPekyprgotZM2jK5KtAY3Ur34svU2Ca8zMNEzFSvtqJusGrEes7MnKTUNrzwtd7SvL4U25jEpsrdAjS3uMWoxu6ynuugv9K+tua6usC+HMtkyJC2pM16yNLy4NO8yjaSZOhS2vrIcrP08Ui3xuow207zCtDooADOqOTAzM7WZNdizHDyks2m1njseOf05d7RYNX84e7lStlpAj71ZsNQ0OTeMuSkyKjUjviGrxreELEU1JLsENo+1Yre/M5eo6LQmvaUuk7C3taI/BjHOsWW1MzkINnU0UzVZIN2wG6nPNNgyCTS7KQAxerWeM9KqOyDgoDQ5yDI0Myq4VK1ALnOz9CRWqBYu66xWMn+z0iSsOIc00LQfrgy0hK3UM/0p/DDVK9C4fDTeuVW8uzV+Nvi0+7JjNF40sbh3QKWxsCmHNbSyHrUwsb21wLLyroux3i28M/KxiTQ3uAK+M6i3rwesljVLLGczAzGNuGAtFC6zLqIlZawIsFejYqCso/auXiwXMtGuPSjfJUcsSS3MJz2qtrYjHk8pHq3DNIOpJrEyJi6smjDyodUmH6veqlGqAbLMw9EswzWfMmg6Ca7sNOerbK/vLEGwM6zysG2cSKisMLk0oatCpkCyiTEAqgYsl6Q8vYeyWa6WrYe8LTF6pOYpMDlhKlunKahdNLMYWqprqOE6ICrWKVcl+iYUK/Gz1CblN0cwzbC/shi1lrAoMK8tOa9zrzuvdh0/s9cvcSE8tNM2vK7RwSwv9jBoqCu09LGCNq0smblNsFu2JbZcrxU1a7IhL0W5O7KGNCM17i6CrTIyLaxiL5WwaCRxqxGxl7R5O7sxH7jesVOxjbGnrcEynS35p9C2YylXrFs02bEWK5xC8jWzNR6y368KNNs2SDEfOKiwdzSgvHmvTbAyr3g1M7BmsU+yiTRAMK0shLdYNQw8ZjdQg3m2tTR3sIw1WzeYuUaz+DDtuNi2ji5dNp41SjWjMgcz/bdmoWCpFLoqtx4//LWKtpm5zTG3L1GyCLjyOigwdbInMm0wy7PgtCOytLJLtk+tdDYGrVgv5rf4sAs+JjbBFhmsfbVwKyWyxKDMNOu06KoWM1O106+EtBWvNChmtJGysDDDqSkwS7iZtThE1y2WtAazPauJNZox+7hCs1u6VLeAqlO82bJfs5G5V7CwtDisnDQIudK2jrQvuUM3xTKzL6MyMiZSKP+2nR/7NGYwZyV0NAG4B7GTttKx4DRBMTQzNjPNqJg3IbS8tfg6iSGtp86xqLSbqDqf6LCzOPGyOqvTqca1y6xqJbiy+7Iop1GpM63JqoUxhLY7tLY3ZKoaKNq1ZLTyKVurZLj/MMi4XbKNsU2rwrXnJl+60LeAtMKlp6fntDMt8qzutN89gTPHLp0wdDRLJmQz0rQrOSs06LGPNRy5ZLLFpzq4niqlmsYwMC6PtE41qzBKnbk1SJm4LHKvirc4qEsoIbNhMiy0dC6rub20oyz5rvcxgLK0LLgumSoItHMvoLJfM5pA1jXWrDUgBbWTK3av0LQjMyKyrbWFtKa097WcuVg1vi4uMH26hLN5LvUww5QLsgNAsbdhN6e4Fpt8M1oxfbUDN6K43LVlu8swy7PYKEswfi2puHoxYbVfrAcxUayZMoc3cTA3sVcwqzQIsSAwVjD8s/SqgTUitH80i7f1MOow7DA4KhElSLKuMlm1IawVnwEwdjP0tFo1h7A/uGYvbLZDvlm3pDWNLeW4Br9yKsAx9CiTn322M7EKMPG5J7NiHro9FzFeoRyo7C1CJTqxDrFXOWoq0jUmMESwIbUgra2jNDTHqr+uwKpNJjaxVDOtLy03ZDK0sOIxnDXPKJo1zjJssdgwZTPLsJgw2hzsNTM0KDG2Mxiw7rVhM4auAi72MvO3yzYGrQc1DLM4KogwJjGAHt0zyjIursW5lqzuNQw03LXQMWYw0bMgrzazXaUAMNMgeR3YF1gyF6musT4xpzTbM4A08DMBNUyv4ygIIo02yjQMNKupkC2NMy4yTq9zHB4z3DKQNQkr37D0rdY3FbaMuEqqQzgJJ226zC2usOIpaKhUqSu34C9hLxOx9LGmtgpFYLAOub+0xbKgM1muoZw7PTi0Y7wzKrIzmrensbkx9i9SMO021aw3LxE4NC+utsm9JjR6Oq8vxDYDsAk3KCr2tpylozZRuVi5ZaUbMcgx2rK5Jx+0QLqssCugxrXhtpM/msDIOHylMzExMKIoP6bFN9G0XzSdMdCptK4AsI4h96dKrDa1jjAlJPUvD7CcuOE4eDQ+Okw9u7Tcseg43Sy9NDUkqzlXvPQxt7DUsH+oAbAKIAS3J64ErPoxyDd0MAI1CrVlPLu1vim9u/m4czBzNac0NDfbNJgr3iyJMT+tFTTdsW+1aKrqrFqkaLTJtqYy3jkRszy4kBzwK/u4uq4ZtU04ozj8NIGrnC49tpgxyjOSNVG4zDMfKXmtWzTQKcU86r1StXo9OrbGoF42N61jvhs7iTw8vBmwzrRROIAzvj1KMq68f7V2sqSvzzkLM3BA37yzuka6DrGus+w6WKTAut+01DwRPOqvHrFtveitvDhwOea8CK4ItLMwjj8psSm3RjoFvuaoczVjN0O5WLK9PHstBD8Os3Wwjy4auKq0LTgHL2+p+jUls20xWD0FrEEx6K/6vUa0HrQfqW+3BbINuRW2b7hjOFe5cDVZuHcmwjjxszW1HrMVseipkjYiskMyDDNrODmyNC9Mt1O7zTKorxexEzwaNEYtnayOrAknYjAQNe85qawyLWK21C77LuWxFTUMMoK1iyqxJpss6inLr3M3wbSGPCispK2PNA8dJDA/tm+vNibMJ8ufWi67rrkyUbG1LVY8HKwOpB21u64WoZ+jRiZyQEKquKyssjUltzYxJJ6wCbadLFapJyIiKPy2XCqxJP+k2hGaMWMlBCDEsnKrxCy1NX4lASVhrOwnxi9Lsd0t5DNHKqIvyrNMswguM7dGLbIxnq5WKwgqF65RMSS0VTXXJi+pj6rGsdIvlx+zN2mwRbHwrCcsS7blKKk2EbKYKscc9y8BKSqxgy8GsAg7JTWkvxgmFCr/tA+cLyqysAA41D8vsqYyijcntOkuezRPLaywDCcMMY4zBKSkOq2yR7oBQDGwV7dUOeqoDqMyKeY7T79ZMDa7zDLUMx2vYDvCIH60nSKBLhO15rAWM8G4U7gqPF2zE7OxtTiuCzonMXkpqrlDqFqx1rYINHmwDDExnFs3k7HEMgS8qLFzuuU5ADa/vqa4HS08vC8sfDoqtwWuFj5Tsnaf0LQ3GWGxxTd5ONWzcBJ6Jwu3PLQJNGWuiDZLOe2ozK27sLAsJjpiMCKvAbiipTW0YS7Cnvu5Wj8ws0K4uy74KUGhurCJti859CZ2MJIsnq3HNTasIC1BtCmyOzHqJDskiLCpKiw737xItF4/Uq5psyO4CatkM1ys8Te9MhEshLRHt4YsQTT4Mi+ta7VdKfWpqrTZNDC+5SoZNi++mDE8OYypyC/PNAO3ZiqcsaqYVDJkJJ0mZrSCsWc4gDMWJSQwSjlCsGuwq7jetPS2PKRkKVg2tLZkOrUvIDhlQFC3CLyUNeMfW6C8raY5trlkqIS+dbWltt06LrC5Nxk41inIMF61nbp3tlI4eDmhwC42ZTU8l5QxDDCgM4O2QDopq8oyy6yfLHewZazctkYpTa3mLqQ2gbOlNSY2/bpPNT2xBTGyqr2jt680Miu4OyFaraUy85y1HYEVsij/HdGa85IynfIWZxnXnYCrUKefG6CXFZAIrjegOpuYND4cCZNSpVCg2Z/KHBkd+BuzpKKhqxinClwomCGynqe1myfJJIsY5yZPJdGkHpkUuBab7KBVJeYUt55YkbUisS5lEgcZxARFmSGjKhkkFbWxLhZkEbySVaWLKyybyBkqNNwYnpUml7cV"), t5 = new Uint8Array(e5.length), r4 = 0; r4 < e5.length; r4++) t5[r4] = e5.charCodeAt(r4);
                  return { buffer: t5.buffer };
                } catch (e6) {
                  return { err: new Sn("Model", Tn.ModelInitFailed, "Failed to read weights binary string: " + ((null == e6 ? void 0 : e6.message) || "unknown"), true) };
                }
              })(), t4 = e4.buffer, r3 = e4.err;
              return r3 || !t4 ? (Be.error("Failed to load local model params", r3), { err: r3 }) : { result: { weightsMap: ni, weightsBinary: t4 } };
            }, r2.capabilties = function() {
              return this.model.capabilities();
            }, t3;
          })(In), oi = (function() {
            function e3(e4, t4, r2) {
              this.windowContext = e4, this.listeners = r2, this.canvas = void 0, this.sourceVideo = void 0, this.pipeline = void 0, this.frameStream = void 0, this.gpuContext = void 0, this.performanceMonitor = void 0, this.config = void 0, this.transformerCapabilities = void 0, this.config = qe({ gpu: { timingInfoEnabled: false, requiredFeatures: ["shader-f16"] }, pipeline: { minimumFramerate: 60, includeReceiveDelay: true }, capture: { maxQueueSize: 2 }, monitor: { enable: true, monitorIntervalMs: 5e3, frameRateDiffPercentage: 0.25, frameCountDiffPercentage: 0.25 }, superRes: { enable: false, loadWidthBytes: 8, models: [] } }, t4), Be.info("[VideoTransformer]: Parsed config", this.config), this.canvas = e4.document.createElement("canvas"), this.transformerCapabilities = { superRes: [] };
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4, r2, n2, i2, o2, a2, s2, u2, c2, l2, d2, f2, h2, p2, v2 = this;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      return t4 = performance.now(), Be.log("[VideoTransformer]: Init start"), r2 = this.onErrorInternal.bind(this), e6.prev = 1, n2 = this.gpuContext = new Pn(this.windowContext, this.config.gpu), e6.next = 2, n2.init();
                    case 2:
                      if (!(i2 = e6.sent)) {
                        e6.next = 3;
                        break;
                      }
                      return e6.abrupt("return", { err: i2 });
                    case 3:
                      return o2 = new An(this.canvas, n2, r2), e6.next = 4, o2.init();
                    case 4:
                      if (!(a2 = e6.sent)) {
                        e6.next = 5;
                        break;
                      }
                      return e6.abrupt("return", { err: a2 });
                    case 5:
                      if (s2 = new wn(this.config.capture, r2), !(u2 = s2.init())) {
                        e6.next = 6;
                        break;
                      }
                      return e6.abrupt("return", { err: u2 });
                    case 6:
                      if (!this.config.superRes.enable) {
                        e6.next = 9;
                        break;
                      }
                      return l2 = new ii(this.config.superRes, n2, { error: r2, capabilities: function(e7) {
                        v2.transformerCapabilities.superRes = e7, v2.listeners.capabilities(v2.transformerCapabilities);
                      } }), e6.next = 7, l2.init();
                    case 7:
                      if (!(d2 = e6.sent)) {
                        e6.next = 8;
                        break;
                      }
                      return e6.abrupt("return", { err: d2 });
                    case 8:
                      c2 = l2, e6.next = 10;
                      break;
                    case 9:
                      c2 = new Dn();
                    case 10:
                      return f2 = new Cn(s2.getStream(), o2, { error: r2 }, this.config.pipeline), e6.next = 11, f2.init(c2);
                    case 11:
                      if (!(h2 = e6.sent)) {
                        e6.next = 12;
                        break;
                      }
                      return e6.abrupt("return", { err: h2 });
                    case 12:
                      return this.performanceMonitor = new Mn(this.config.monitor, this.stats.bind(this), { performanceBreach: this.onPerformanceBreach.bind(this) }), this.frameStream = s2, this.pipeline = f2, Be.log("[VideoTransformer]: Init complete in " + (performance.now() - t4).toFixed(3) + "ms"), e6.abrupt("return", { info: { timeMs: performance.now() - t4, gpuProperties: n2.properties() } });
                    case 13:
                      return e6.prev = 13, p2 = e6.catch(1), Be.warn("[VideoTransformer]: Failed to init", p2), e6.abrupt("return", { err: new Sn("VideoTransformer", Tn.Init, p2.message, true) });
                    case 14:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[1, 13]]);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.start = function(e4) {
              var t4, r2;
              this.ready() ? this.active() ? Be.warn("[VideoTransformer]: Start called on active instance") : (this.sourceVideo = e4, null == (t4 = this.frameStream) || t4.start(e4), null == (r2 = this.performanceMonitor) || r2.start(), Be.log("[VideoTransformer]: Pipeline started")) : Be.log("[VideoTransformer]: Start called before init");
            }, t3.stop = function() {
              var e4, t4;
              Be.log("[VideoTransformer]: Stopping render pipeline"), null == (e4 = this.frameStream) || e4.stop(), null == (t4 = this.performanceMonitor) || t4.stop();
            }, t3.setVideoFrameMetadata = function(e4) {
              var t4;
              null == (t4 = this.frameStream) || t4.setVideoFrameMetadata(e4);
            }, t3.active = function() {
              var e4, t4;
              return null != (e4 = null == (t4 = this.frameStream) ? void 0 : t4.active) && e4;
            }, t3.stats = function() {
              var e4, t4, r2, n2, i2, o2;
              if (!this.pipeline || !this.frameStream) return { timestamp: performance.now(), frames: { source: 0, source_presented: 0, captured: 0, received: 0, transformed: 0, rendered: 0, missed: 0, blocked: 0, skipped: 0, failed: 0, overBudget: 0 }, timings: { captureDelay: 0, receiveDelay: 0, transformTime: 0, renderTime: 0, endToEnd: 0 }, resolution: { sourceHeight: 0, sourceWidth: 0, renderHeight: 0, renderWidth: 0 } };
              var a2 = this.pipeline.stats(), s2 = a2.received, u2 = a2.transformTime, c2 = a2.skipped, l2 = a2.transformed, d2 = a2.failed, f2 = a2.overBudget, h2 = a2.rendered, p2 = a2.renderTime, v2 = a2.receiveDelay, g2 = a2.endToEnd, m2 = this.frameStream.stats(), y2 = m2.captured, b2 = m2.blocked, E2 = m2.missed, S2 = m2.rVFCDelay, T2 = null != (e4 = null == (t4 = this.sourceVideo) ? void 0 : t4.getVideoPlaybackQuality()) ? e4 : { totalVideoFrames: 0, droppedVideoFrames: 0 }, _2 = T2.totalVideoFrames, C2 = T2.droppedVideoFrames;
              return { timestamp: performance.now(), frames: { source: _2, source_presented: _2 - C2, captured: y2, received: s2, transformed: l2, rendered: h2, missed: E2, blocked: b2, skipped: c2, failed: d2, overBudget: f2 }, timings: { captureDelay: S2, receiveDelay: v2, transformTime: u2, renderTime: p2, endToEnd: g2 }, resolution: { sourceHeight: null != (r2 = null == (n2 = this.sourceVideo) ? void 0 : n2.videoHeight) ? r2 : 0, sourceWidth: null != (i2 = null == (o2 = this.sourceVideo) ? void 0 : o2.videoWidth) ? i2 : 0, renderHeight: this.canvas.height, renderWidth: this.canvas.width } };
            }, t3.renderSurface = function() {
              return this.canvas;
            }, t3.gpuProperties = function() {
              var e4;
              return null == (e4 = this.gpuContext) ? void 0 : e4.properties();
            }, t3.capabilities = function() {
              return this.transformerCapabilities;
            }, t3.reset = function() {
              var e4, t4, r2;
              Be.info("[VideoTransformer]: Resetting instance"), null == (e4 = this.frameStream) || e4.resetStats(), null == (t4 = this.pipeline) || t4.resetStats(), null != (r2 = this.performanceMonitor) && r2.monitoring() && (this.performanceMonitor.stop(), this.performanceMonitor.start());
            }, t3.destroy = function() {
              Be.info("[VideoTransformer]: Destroying instance"), this.stop(), this.transformerCapabilities = { superRes: [] }, this.listeners.capabilities(this.transformerCapabilities), this.gpuContext = void 0, this.frameStream = void 0, this.pipeline = void 0, this.performanceMonitor = void 0;
            }, t3.ready = function() {
              return !!(this.frameStream && this.pipeline && this.performanceMonitor && this.gpuContext);
            }, t3.onPerformanceBreach = function(e4) {
              this.onErrorInternal(new Sn("PerformanceMonitor", e4.code, e4.reason, e4.fatal));
            }, t3.onErrorInternal = function(e4) {
              this.stop(), this.reset(), this.listeners.error(e4);
            }, e3;
          })(), ai = (function() {
            function e3(e4, t4) {
              var r2;
              this.initialConfig = e4, this.listeners = t4, this.videoTransformer = void 0, this.surface = void 0, this.statusInterval = void 0, this.scoutModeTimeout = void 0, this.config = void 0, this.disabled = false, this.scouted = false, this.resumeOnVisible = false;
              var n2 = { statusIntervalMs: 5e3, scoutPeriodMs: 5500, logLevel: Ve.WARN, gpuAllowlist: { vendors: [], architectures: [] }, behaviors: {}, transformer: {} };
              this.config = qe(n2, e4.module);
              var i2 = null == (r2 = this.config.transformer.monitor) ? void 0 : r2.monitorIntervalMs;
              void 0 === e4.module.scoutPeriodMs && i2 && (this.config.scoutPeriodMs = i2 + 500), Be.setConfigByLevel(this.config.logLevel), Be.info("[VideoTransformerModule]: Parsed config", this.config);
            }
            var t3 = e3.prototype;
            return t3.init = (function() {
              var e4 = Ne()(Ue().mark(function e5() {
                var t4, r2, n2, i2;
                return Ue().wrap(function(e6) {
                  for (; ; ) switch (e6.prev = e6.next) {
                    case 0:
                      if (!this.videoTransformer) {
                        e6.next = 1;
                        break;
                      }
                      return Be.debug("[VideoTransformerModule]: Init called on setup instance"), e6.abrupt("return", void 0);
                    case 1:
                      if (!this.disabled) {
                        e6.next = 2;
                        break;
                      }
                      return Be.warn("[VideoTransformerModule]: Init called, but the module is disabled"), e6.abrupt("return", void 0);
                    case 2:
                      return e6.prev = 2, t4 = new oi(window, this.config.transformer, { error: this.onTransformerError.bind(this), capabilities: this.onTransformerCapabilities.bind(this) }), e6.next = 3, t4.init();
                    case 3:
                      if (!(r2 = e6.sent).err) {
                        e6.next = 4;
                        break;
                      }
                      return this.onTransformerError(r2.err), e6.abrupt("return", void 0);
                    case 4:
                      if (this.videoTransformer = t4, n2 = this.gpuAllowed(), this.logInitEvent(r2, n2), n2) {
                        e6.next = 5;
                        break;
                      }
                      return this.disable(), e6.abrupt("return", void 0);
                    case 5:
                      return this.surface = this.createWebGpuCanvasSurface(t4.renderSurface()), e6.abrupt("return", this.surface);
                    case 6:
                      return e6.prev = 6, i2 = e6.catch(2), Be.error("[VideoTransformerModule]: Unhandled error encountered during init", i2), this.onTransformerError(new Sn("VideoTransformerModule", Tn.Init, "Unhandled error during init: " + ((null == i2 ? void 0 : i2.message) || "unknown"), true)), e6.abrupt("return", void 0);
                    case 7:
                    case "end":
                      return e6.stop();
                  }
                }, e5, this, [[2, 6]]);
              }));
              return function() {
                return e4.apply(this, arguments);
              };
            })(), t3.start = function(e4) {
              if (this.disabled) Be.log("[VideoTransformerModule]: Video transformer not starting: module is disabled");
              else if (this.videoTransformer) if (this.videoTransformer.active()) Be.log("[VideoTransformerModule]: Start called, but transformer is already active");
              else {
                if (ci()) return Be.log("[VideoTransformerModule]: Start called, but page is hidden. Resuming on visible"), void (this.resumeOnVisible = true);
                Be.log("[VideoTransformerModule]: Starting"), this.videoTransformer.start(e4), this.videoTransformer.active() ? (this.startLogStatusEvents(this.config.statusIntervalMs), this.scout(this.config.scoutPeriodMs)) : (Be.log("[VideoTransformerModule]: Failed to start"), this.onTransformerError(new Sn("VideoTransformerModule", Tn.BadParameters, "Transformer failed to start", true)));
              }
              else Be.log("[VideoTransformerModule]: Video transformer not starting: start called before transformer init");
            }, t3.stats = function() {
              var e4, t4, r2 = 0, n2 = 0, i2 = performance.now(), o2 = null == (e4 = this.videoTransformer) ? void 0 : e4.stats().frames;
              return o2 ? (r2 = null != (t4 = o2.rendered) ? t4 : 0, { timestamp: i2, droppedFrames: n2 = o2.missed + o2.skipped + o2.failed, decodedFrames: r2 }) : { timestamp: i2, decodedFrames: r2, droppedFrames: n2 };
            }, t3.swapVideoSource = function(e4) {
              var t4;
              null != (t4 = this.videoTransformer) && t4.active() && (this.videoTransformer.stop(), this.videoTransformer.start(e4));
            }, t3.stop = function() {
              var e4;
              null == (e4 = this.videoTransformer) || e4.stop(), this.resumeOnVisible = false, clearInterval(this.statusInterval), clearInterval(this.scoutModeTimeout);
            }, t3.reset = function() {
              var e4;
              null == (e4 = this.videoTransformer) || e4.reset();
            }, t3.destroy = function() {
              var e4;
              this.stop(), this.listeners.syntheticQualities([]), null == (e4 = this.videoTransformer) || e4.destroy(), this.videoTransformer = void 0, this.listeners.inactive();
            }, t3.disable = function() {
              this.disabled = true, this.destroy();
            }, t3.onPageVisibilityChanged = function(e4, t4) {
              if (e4) {
                var r2, n2, i2 = null != (r2 = null == (n2 = this.videoTransformer) ? void 0 : n2.active()) && r2;
                this.stop(), Be.log("[VideoTransformerModule]: Page hidden. Was active: " + i2), this.resumeOnVisible = i2;
              } else Be.log("[VideoTransformerModule]: Page visible, resumeOnVisible: " + this.resumeOnVisible), this.resumeOnVisible && this.start(t4), this.resumeOnVisible = false;
            }, t3.onVideoSessionChanged = function() {
              var e4;
              Be.log("[VideoTransformerModule]: onVideoSession, initialized: " + !!this.videoTransformer);
              var t4 = null == (e4 = this.videoTransformer) ? void 0 : e4.gpuProperties();
              if (t4) {
                var r2 = { info: { timeMs: 0, gpuProperties: t4 } };
                this.logInitEvent(r2, this.gpuAllowed());
              }
            }, t3.onQualityChanged = function(e4) {
              var t4, r2, n2, i2, o2, a2 = null != (t4 = null == (r2 = e4.attributes) ? void 0 : r2.Pipeline) ? t4 : "", s2 = null == (n2 = this.videoTransformer) ? void 0 : n2.capabilities().superRes.find(function(e5) {
                return e5.id === a2;
              });
              if (!s2) return Be.warn("[VideoTransformerModule]: Failed to find capability for " + a2 + " on quality " + e4.name + " received"), void (null == (o2 = this.videoTransformer) || o2.setVideoFrameMetadata({ pipelineId: "", match: { width: -1, height: -1 } }));
              null == (i2 = this.videoTransformer) || i2.setVideoFrameMetadata({ pipelineId: a2, match: { width: s2.widthFrom, height: s2.heightFrom } });
            }, t3.scout = function(e4) {
              var t4 = this;
              window.clearTimeout(this.scoutModeTimeout), this.scouted ? Be.log("[VideoTransformerModule]: Already scouted") : (Be.log("[VideoTransformerModule]: Starting " + e4 + "ms scout mode before ready"), this.scoutModeTimeout = window.setTimeout(function() {
                var e5;
                t4.scouted = true, null != (e5 = t4.videoTransformer) && e5.active() ? (Be.log("[VideoTransformerModule]: Scout mode complete"), t4.onTransformerCapabilities(t4.videoTransformer.capabilities()), t4.listeners.active()) : Be.log("[VideoTransformerModule]: Scout mode complete, but video transformer is no longer active");
              }, e4));
            }, t3.gpuAllowed = function() {
              var e4, t4 = null == (e4 = this.videoTransformer) ? void 0 : e4.gpuProperties();
              if (!t4) return Be.info("[VideoTransformerModule]: GPU not allowed: gpu properties not found"), false;
              var r2, n2, i2, o2, a2, s2 = (r2 = this.config.gpuAllowlist, o2 = [].concat(null != (n2 = null == r2 ? void 0 : r2.vendors) ? n2 : []), a2 = [].concat(null != (i2 = null == r2 ? void 0 : r2.architectures) ? i2 : []), { allowed: function(e5) {
                var t5 = { allowed: true, reason: "" };
                return o2.includes(e5.vendor) || o2.includes("*") || (t5 = { allowed: false, reason: "GPU Vendor not allowed. Have: " + e5.vendor + " Need: " + o2.join(",") }), a2.includes(e5.architecture) || a2.includes("*") || (t5 = { allowed: false, reason: "GPU Architecture not allowed. Have: " + e5.architecture + " Need: " + a2.join(",") }), t5;
              } }), u2 = s2.allowed(t4);
              return !!u2.allowed || (Be.info("[VideoTransformerModule]: GPU not allowed: " + u2.reason), false);
            }, t3.createWebGpuCanvasSurface = function(e4) {
              var t4 = (function(e5) {
                return e5.className = "ivs-player-canvas-surface", e5.style.objectFit = "contain", e5.style.overflowClipMargin = "content-box", e5.style.overflow = "clip", e5.style.imageRendering = "auto", { element: function() {
                  return e5;
                }, displayDimensions: function() {
                  return { width: e5.clientWidth, height: e5.clientHeight };
                }, videoDimensions: function() {
                  return { width: e5.clientWidth, height: e5.clientHeight };
                }, stats: function() {
                  return { timestamp: performance.now(), droppedFrames: 0, decodedFrames: 0 };
                }, visible: function() {
                  return x(e5);
                } };
              })(e4);
              return t4.stats = this.stats.bind(this), t4;
            }, t3.startLogStatusEvents = function(e4) {
              var t4, r2, n2 = this, i2 = null != (t4 = null == (r2 = this.videoTransformer) ? void 0 : r2.stats()) ? t4 : ui();
              clearInterval(this.statusInterval), this.statusInterval = window.setInterval(function() {
                var t5, r3;
                if (n2.videoTransformer) {
                  var o2 = n2.videoTransformer.stats(), a2 = si(i2, o2), s2 = a2.frames, u2 = a2.timings, c2 = a2.timestamp, l2 = a2.resolution, d2 = 0, h2 = 0, p2 = 0, v2 = 0, g2 = (o2.timestamp - i2.timestamp) / 1e3;
                  g2 > 0 && (d2 = a2.frames.source / g2, h2 = a2.frames.source_presented / g2, p2 = a2.frames.captured / g2, v2 = a2.frames.rendered / g2), n2.printStats(a2, d2, h2, p2, v2);
                  var m2 = f()({ status_timestamp: c2, status_interval: e4, surface_visible: null != (t5 = null == (r3 = n2.surface) ? void 0 : r3.visible()) && t5, source_height: l2.sourceHeight, source_width: l2.sourceWidth, render_height: l2.renderHeight, render_width: l2.renderWidth, frames_source: s2.source, frames_source_presented: s2.source_presented, frames_captured: s2.captured, frames_missed: s2.missed, frames_blocked: s2.blocked, frames_skipped: s2.skipped, frames_transformed: s2.transformed, frames_rendered: s2.rendered, frames_overbudget: s2.overBudget, frames_failed: s2.failed, framerate_source: d2, framerate_source_presented: h2, framerate_capture: p2, framerate_render: v2, time_capture_delay: u2.captureDelay, time_receive_delay: u2.receiveDelay, time_transform: u2.transformTime, time_render: u2.renderTime, time_end_to_end: u2.endToEnd }, n2.gpuAnalyticsProperties());
                  i2 = o2, n2.listeners.analytics({ name: "onGpuStatus", data: m2 });
                } else window.clearInterval(n2.statusInterval);
              }, e4);
            }, t3.logInitEvent = function(e4, t4) {
              if (e4.info) {
                var r2 = e4.info, n2 = r2.timeMs, i2 = r2.gpuProperties, o2 = f()({ gpu_time_init: n2, gpu_adapter_features: i2.adapterFeatures, gpu_device_features: i2.deviceFeatures, gpu_wgsl_features: i2.wgslFeatures, gpu_transformer_config: JSON.stringify(this.initialConfig), gpu_allowed: t4 }, this.gpuAnalyticsProperties());
                this.listeners.analytics({ name: "onGpuInit", data: o2 });
              }
            }, t3.onTransformerCapabilities = function(e4) {
              var t4 = this;
              if (this.videoTransformer && this.scouted) if (this.gpuAllowed() && !this.disabled) {
                var r2 = [], n2 = { id: "", widthFrom: -1, heightFrom: -1, widthTo: -1, heightTo: -1, framerateFrom: -1, codec: "", encoder: "", scoreModifier: 0, behavior: { nameOverride: "", mode: "none" } };
                e4.superRes.forEach(function(e5) {
                  var i2 = f()({}, n2, e5, { behavior: f()({}, n2.behavior, t4.config.behaviors[e5.id]) });
                  r2.push(i2);
                }), Be.info("[VideoTransformerModule]: Sending synthetics", r2), this.listeners.syntheticQualities(r2);
              } else Be.info("[VideoTransformerModule]: Not sending synthetics because the module is disabled, or the GPU is not allowlisted");
              else Be.info("[VideoTransformerModule]: Not sending synthetics because the module has not finished initializing or scouting");
            }, t3.onTransformerError = function(e4) {
              var t4, r2, n2 = f()({ code: e4.code, source: e4.source, message: e4.message, fatal: true, surface_visible: null != (t4 = null == (r2 = this.surface) ? void 0 : r2.visible()) && t4 }, this.gpuAnalyticsProperties());
              Be.warn("[VideoTransformerModule]: Error, disabling module", e4), this.listeners.error(n2), this.listeners.analytics({ name: "onGpuError", data: n2 }), this.destroy();
            }, t3.gpuAnalyticsProperties = function() {
              var e4, t4, r2, n2, i2, o2 = null == (e4 = this.videoTransformer) ? void 0 : e4.gpuProperties();
              return { gpu_architecture: null != (t4 = null == o2 ? void 0 : o2.architecture) ? t4 : "", gpu_description: null != (r2 = null == o2 ? void 0 : o2.description) ? r2 : "", gpu_device: null != (n2 = null == o2 ? void 0 : o2.device) ? n2 : "", gpu_vendor: null != (i2 = null == o2 ? void 0 : o2.vendor) ? i2 : "" };
            }, t3.printStats = function(e4, t4, r2, n2, i2) {
              var o2, a2, s2 = e4.frames, u2 = s2.source, c2 = s2.source_presented, l2 = s2.captured, d2 = s2.received, f2 = s2.transformed, h2 = s2.rendered, p2 = s2.missed, v2 = s2.blocked, g2 = s2.skipped, m2 = s2.failed, y2 = s2.overBudget, b2 = e4.timings, E2 = b2.captureDelay, S2 = b2.receiveDelay, T2 = b2.transformTime / f2, _2 = b2.renderTime / h2, C2 = S2 / l2, k2 = b2.endToEnd / h2, w2 = E2 / l2, P2 = "Stats:\nStats Interval:                        " + this.config.statusIntervalMs + "ms\nDocument visible:                      " + !document.hidden + "\nSurface visible:                       " + (null != (o2 = null == (a2 = this.surface) ? void 0 : a2.visible()) && o2) + "\nSource Resolution                      " + e4.resolution.sourceWidth + "x" + e4.resolution.sourceHeight + "\nRender Resolution                      " + e4.resolution.renderWidth + "x" + e4.resolution.renderHeight + "\nSource Total Framerate:                " + t4.toFixed(1) + "\nSource Presented Framerate:            " + r2.toFixed(1) + "\nCapture Framerate:                     " + n2.toFixed(1) + "\nRender Framerate:                      " + i2.toFixed(1) + "\nSource Total frames:                   " + u2 + "\nSource Presented frames:               " + c2 + "\nSource Dropped frames:                 " + (u2 - c2) + "\nCaptured frames:                       " + l2 + "\nReceived frames:                       " + d2 + "\nTransformed frames:                    " + f2 + "\nRendered frames:                       " + h2 + "\nMissed frames:                         " + p2 + "\nBlocked frames:                        " + v2 + "\nSkipped frames:                        " + g2 + "\nOverbudget frames:                     " + y2 + "\nFailed frames:                         " + m2 + "\nAverage rVFC capture delay:            " + w2.toFixed(3) + "ms\nAverage frame receive delay:           " + C2.toFixed(3) + "ms\nAverage frame transform time:          " + T2.toFixed(3) + "ms\nAverage frame render time:             " + _2.toFixed(3) + "ms\nAverage frame end-to-end time:         " + k2.toFixed(3) + "ms\n";
              Be.log("[VideoTransformerModule]: " + P2);
            }, e3;
          })(), si = function(e3, t3) {
            var r2 = { timestamp: t3.timestamp, resolution: f()({}, t3.resolution), frames: { source: 0, source_presented: 0, captured: 0, received: 0, transformed: 0, rendered: 0, missed: 0, blocked: 0, skipped: 0, failed: 0, overBudget: 0 }, timings: { captureDelay: 0, receiveDelay: 0, transformTime: 0, renderTime: 0, endToEnd: 0 } };
            return ["frames", "timings"].forEach(function(n2) {
              for (var i2 = 0, o2 = Object.entries(t3[n2]); i2 < o2.length; i2++) {
                var a2 = o2[i2], s2 = a2[0], u2 = a2[1];
                r2[n2][s2] = u2 - e3[n2][s2];
              }
            }), r2;
          }, ui = function() {
            return { timestamp: performance.now(), frames: { source: 0, source_presented: 0, captured: 0, received: 0, transformed: 0, rendered: 0, missed: 0, blocked: 0, skipped: 0, failed: 0, overBudget: 0 }, timings: { captureDelay: 0, receiveDelay: 0, transformTime: 0, renderTime: 0, endToEnd: 0 }, resolution: { sourceHeight: 0, sourceWidth: 0, renderHeight: 0, renderWidth: 0 } };
          }, ci = function() {
            var e3 = A(document).hidden;
            return document[e3];
          }, li = (function(e3) {
            return e3[e3.STATE_CHANGED = 0] = "STATE_CHANGED", e3[e3.CONFIGURE = 1] = "CONFIGURE", e3[e3.RESET = 2] = "RESET", e3[e3.ADD_CUE = 3] = "ADD_CUE", e3[e3.GET_DECODE_INFO = 4] = "GET_DECODE_INFO", e3[e3.MEDIA_SINK_RPC = 5] = "MEDIA_SINK_RPC", e3[e3.GET_EXPERIMENTS = 6] = "GET_EXPERIMENTS", e3[e3.LOG_MESSAGE = 7] = "LOG_MESSAGE", e3[e3.DATA_CHANNEL_CREATE = 8] = "DATA_CHANNEL_CREATE", e3[e3.DATA_CHANNEL_CLOSE = 9] = "DATA_CHANNEL_CLOSE", e3[e3.DATA_CHANNEL_SEND = 10] = "DATA_CHANNEL_SEND", e3[e3.RTC_SET_REMOTE_DESCRIPTION = 11] = "RTC_SET_REMOTE_DESCRIPTION", e3[e3.PROPERTY_CHANGED = 12] = "PROPERTY_CHANGED", e3[e3.SYNC_TIME_CHANGED = 13] = "SYNC_TIME_CHANGED", e3[e3.BUFFERED_RANGES = 14] = "BUFFERED_RANGES", e3[e3.AD_BREAK_STARTED = 15] = "AD_BREAK_STARTED", e3[e3.AD_CREATIVE_STARTED = 16] = "AD_CREATIVE_STARTED", e3[e3.AD_TIME_UPDATE = 17] = "AD_TIME_UPDATE", e3[e3.AD_CREATIVE_ENDED = 18] = "AD_CREATIVE_ENDED", e3[e3.AD_BREAK_ENDED = 19] = "AD_BREAK_ENDED", e3[e3.DESTROY = 20] = "DESTROY", e3;
          })({});
          function di(e3, t3) {
            var r2 = "undefined" != typeof Symbol && e3[Symbol.iterator] || e3["@@iterator"];
            if (r2) return (r2 = r2.call(e3)).next.bind(r2);
            if (Array.isArray(e3) || (r2 = (function(e4, t4) {
              if (e4) {
                if ("string" == typeof e4) return fi(e4, t4);
                var r3 = {}.toString.call(e4).slice(8, -1);
                return "Object" === r3 && e4.constructor && (r3 = e4.constructor.name), "Map" === r3 || "Set" === r3 ? Array.from(e4) : "Arguments" === r3 || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(r3) ? fi(e4, t4) : void 0;
              }
            })(e3)) || t3 && e3 && "number" == typeof e3.length) {
              r2 && (e3 = r2);
              var n2 = 0;
              return function() {
                return n2 >= e3.length ? { done: true } : { done: false, value: e3[n2++] };
              };
            }
            throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.");
          }
          function fi(e3, t3) {
            (null == t3 || t3 > e3.length) && (t3 = e3.length);
            for (var r2 = 0, n2 = Array(t3); r2 < t3; r2++) n2[r2] = e3[r2];
            return n2;
          }
          var hi = (function() {
            function e3(t4, r2) {
              var n2, i2, o2, a2, s2, u2 = this;
              this.worker = void 0, this.id = void 0, this.emitter = new tr(), this.seekTime = null, this.paused = true, this.isLoaded = false, this.autoPlayOptions = null, this.mediaSinkManager = void 0, this.experiments = {}, this.adjustments = void 0, this.enableRemoteSearch = void 0, this.isQualitySupported = void 0, this.onvisibilitychange = void 0, this.onmessage = function(e4) {
                return u2.onWorkerMessage(e4);
              }, this.onOnline = function() {
                return u2.postMessage("onOnline");
              }, this.onOffline = function() {
                return u2.postMessage("onOffline");
              }, this.pauseHiddenSilentTab = void 0, this.state = void 0, this.analyticsPropertiesProcessors = [], this.configManager = void 0, this.deviceConfigMetricsHelper = vt.getInstance(), this.deviceConfigEmitMetricsBound = this.emitDeviceConfigManagerMetrics.bind(this), this.daterangeTagAssembler = void 0, this.queuedDeviceConfigEvents = [], this.isReady = false, this.videoTransformerModule = void 0, this.renderSurface = new $t(document), this.logger = void 0, this.startCapture = void 0, this.stopCapture = void 0, this.requestCaptureAnalytics = void 0, this.logger = (0, K.createLogger)({ name: "mediaplayer-core" }), e3.instanceCount += 1, this.worker = r2, this.id = e3.instanceId++, this.isQualitySupported = t4.isQualitySupported || P, this.onvisibilitychange = function() {
                return u2.onVisibilityChange();
              }, this.enableRemoteSearch = t4.enableRemoteSearch || false, void 0 !== t4.logLevel && (0, K.setLogConfigByLevel)({ enabled: true, level: t4.logLevel }), void 0 !== t4.logCategories && (0, K.setLogConfigByCategories)(t4.logCategories);
              var c2, l2, d2 = xe();
              void 0 !== t4.platform && null !== t4.platform && "" !== t4.platform && (d2.platform = null != (c2 = t4.platform) && Object.values(Ce).includes(c2) ? c2 : Ce.DESKTOP), this.pauseHiddenSilentTab = d2.chrome && 63 === d2.major || d2.opera, this.adjustments = (l2 = null != (n2 = null == (i2 = t4.webviewHost) ? void 0 : i2.adjustments) ? n2 : {}, f()({}, Ye, l2)), this.mediaSinkManager = new gn(this, this.enableRemoteSearch, this.adjustments, this.renderSurface.videoElement());
              for (var h2, p2, m2, y2, b2, E2, S2, T2 = 0, _2 = [(b2 = null, E2 = null, { init: function() {
                if (null === b2 || null === E2) try {
                  var e4 = document.createElement("canvas"), t5 = e4.getContext("webgl") || e4.getContext("experimental-webgl");
                  if (t5 && "getExtension" in t5) {
                    var r3 = t5.getExtension("WEBGL_debug_renderer_info");
                    r3 && "getParameter" in t5 && (b2 = t5.getParameter(r3.UNMASKED_RENDERER_WEBGL), E2 = t5.getParameter(r3.UNMASKED_VENDOR_WEBGL));
                  }
                } catch (e5) {
                }
              }, getProperties: function() {
                return { gl_renderer: b2, gl_vendor: E2 };
              } }), (m2 = { architecture: "", description: "", device: "", vendor: "" }, y2 = false, { init: function() {
                return Ne()(Ue().mark(function e4() {
                  var t5, r3;
                  return Ue().wrap(function(e5) {
                    for (; ; ) switch (e5.prev = e5.next) {
                      case 0:
                        if (y2 = false, p2 = He.gpu_pending, e5.prev = 1, navigator.gpu) {
                          e5.next = 2;
                          break;
                        }
                        return Be.warn(He.gpu_unavailable), p2 = He.gpu_unavailable, e5.abrupt("return");
                      case 2:
                        return e5.next = 3, navigator.gpu.requestAdapter();
                      case 3:
                        if (t5 = e5.sent) {
                          e5.next = 4;
                          break;
                        }
                        return Be.warn(He.gpu_adapter_unavailable), p2 = He.gpu_adapter_unavailable, e5.abrupt("return");
                      case 4:
                        if (t5.info) {
                          e5.next = 5;
                          break;
                        }
                        return Be.warn(He.gpu_adapterinfo_undefined), p2 = He.gpu_adapterinfo_undefined, e5.abrupt("return");
                      case 5:
                        m2 = t5.info, p2 = void 0, y2 = true, e5.next = 7;
                        break;
                      case 6:
                        e5.prev = 6, r3 = e5.catch(1), Be.warn(He.gpu_error, r3), p2 = He.gpu_error + " " + ((null == r3 ? void 0 : r3.message) || "unknown");
                      case 7:
                      case "end":
                        return e5.stop();
                    }
                  }, e4, null, [[1, 6]]);
                }))();
              }, getProperties: function() {
                return { gpu_supported: y2, gpu_unsupported_reason: p2, gpu_architecture: m2.architecture, gpu_description: m2.description, gpu_device: m2.device, gpu_vendor: m2.vendor };
              } }), { init: function() {
                return Ne()(Ue().mark(function e4() {
                  var t5;
                  return Ue().wrap(function(e5) {
                    for (; ; ) switch (e5.prev = e5.next) {
                      case 0:
                        if (void 0 === h2) {
                          e5.next = 1;
                          break;
                        }
                        return e5.abrupt("return");
                      case 1:
                        if (null == (t5 = window) || null == (t5 = t5.navigator) || !t5.getBattery) {
                          e5.next = 5;
                          break;
                        }
                        return e5.prev = 2, e5.next = 3, window.navigator.getBattery();
                      case 3:
                        h2 = e5.sent, e5.next = 5;
                        break;
                      case 4:
                        e5.prev = 4, e5.catch(2), h2 = void 0;
                      case 5:
                      case "end":
                        return e5.stop();
                    }
                  }, e4, null, [[2, 4]]);
                }))();
              }, getProperties: function() {
                var e4;
                return { battery_percent: null == (e4 = h2) ? void 0 : e4.level };
              } }]; T2 < _2.length; T2++) {
                var C2 = _2[T2];
                C2.init(), this.analyticsPropertiesProcessors.push(C2);
              }
              void 0 !== t4.serviceWorker && ("serviceWorker" in navigator || (cr.warn("Service workers are not supported."), 0)) && ((function(e4) {
                ur = fr(e4);
              })(t4.serviceWorker), null == (S2 = lr()) || S2.registerAndActivate()), this.state = bn(), this.resetState(), this.attachHandlers(), this.checkDebugCaptureEnabled();
              for (var k2, w2 = ((o2 = {})[We.AVC] = true, o2[We.HEVC] = (k2 = 'video/mp4; codecs="hvc1.1.40000000.L60.80.0.0.0.0.0"', v() ? MediaSource.isTypeSupported(k2) : "" !== document.createElement("video").canPlayType(k2)), o2[We.AV1] = (function() {
                var e4 = 'video/mp4; codecs="av01.0.05M.08"';
                return v() ? MediaSource.isTypeSupported(e4) : "" !== document.createElement("video").canPlayType(e4);
              })(), o2), I2 = null != (a2 = null == (s2 = t4.mediaConfig) || null == (s2 = s2.codecConfigs) ? void 0 : s2.length) ? a2 : 0, D2 = 0; D2 < I2; ++D2) {
                var x2, M2 = null == (x2 = t4.mediaConfig) || null == (x2 = x2.codecConfigs) ? void 0 : x2[D2];
                M2 && M2.setting.skipPlatformSupportChecks && (w2[M2.codecString] = !M2.setting.disableUse);
              }
              var R2 = [We.AVC];
              w2[We.HEVC] && R2.push(We.HEVC), w2[We.AV1] && R2.push(We.AV1), this.configManager = new ht({ browserContext: d2, deviceConfig: t4.deviceConfig, sdkVersion: "1.55.0" }, t4, this.onConfigChanged.bind(this)), void 0 !== t4.supportsMuxedFMP4 && (d2.supportsMuxedFMP4 = t4.supportsMuxedFMP4), this.configManager.initDeviceConfigManager({ canRefreshNow: e3.canDeviceConfigManagerRefreshNowCallback, emitMetrics: e3.emitDeviceConfigManagerMetricsCallback, emitAnalytics: function(e4, t5) {
                u2.sendConfigManagerDeviceConfigAnalytics(e4, t5);
              } }), this.configManager.updateConfigFromDeviceConfig() || this.onConfigChanged();
              var L2 = this.configManager.getConfigSnapshot();
              this.postMessage("create", [{ mseSupported: v(), managedMseSupported: g(L2.media), keySystem: void 0 !== t4.keySystem ? t4.keySystem : Ot(d2), browserContext: f()({}, d2, { webviewHost: t4.webviewHost }), codecs: R2, testOnly: t4.testOnly, playerFramework: t4.playerFramework, buildDistId: "npm" }, L2]);
              var O2 = A(document).hidden;
              this.postMessage("setVisible", [!document[O2]]), this.deviceConfigMetricsHelper.registerMetricsCallback(this.deviceConfigEmitMetricsBound), this.emitDeviceConfigManagerMetrics();
            }
            var t3 = e3.prototype;
            return t3.delete = function() {
              var t4, r2, n2 = this;
              this.flushQueuedDeviceConfigEvents(), null == (t4 = lr()) || t4.destroy();
              var i2 = A(document).visibilityChange;
              document.removeEventListener(i2, this.onvisibilitychange), window.removeEventListener("online", this.onOnline), window.removeEventListener("offline", this.onOffline), this.emitter.removeAllListeners(), this.emitter.on(li.DESTROY, function() {
                try {
                  n2.mediaSinkManager.destroy();
                } catch (e4) {
                  n2.onSinkRecoverableError({ value: -1, code: 0, message: e4.message });
                }
                n2.emitter.removeAllListeners(), n2.worker.removeEventListener("message", n2.onmessage);
              }), this.configManager.delete(), null == (r2 = this.videoTransformerModule) || r2.destroy(), this.videoTransformerModule = void 0, this.postMessage("delete"), e3.instanceCount > 0 && (e3.instanceCount -= 1), this.deviceConfigMetricsHelper.unregisterMetricsCallback(this.deviceConfigEmitMetricsBound);
            }, t3.attachHTMLVideoElement = function(e4) {
              var t4;
              try {
                var r2;
                null == (r2 = this.mediaSinkManager) || r2.destroy();
              } catch (e5) {
                this.onSinkRecoverableError({ value: -1, code: -1, message: e5.message });
              }
              this.mediaSinkManager = new gn(this, this.enableRemoteSearch, this.adjustments, e4), this.processVideoElementAttributes(e4), this.renderSurface.setVideo(e4), null == (t4 = this.videoTransformerModule) || t4.swapVideoSource(e4), this.configManager.getConfigSnapshot().features.gpu.flags.enable_render_surface && this.renderSurface.moveVideoToRenderSurface();
            }, t3.getHTMLVideoElement = function() {
              return this.renderSurface.videoElement();
            }, t3.getVideoRenderSurface = function() {
              if (this.configManager.getConfigSnapshot().features.gpu.flags.enable_render_surface) return this.renderSurface.surface();
            }, t3.load = function(e4, t4) {
              var r2 = {};
              null !== t4 && "object" == typeof t4 ? r2 = t4 : "string" == typeof t4 && (r2.mediaType = t4), this.loadWithConfig(e4, r2);
            }, t3.loadWithConfig = function(e4, t4) {
              var r2, n2 = this.analyticsPropertiesProcessors.reduce(function(e5, t5) {
                return Object.assign(e5, t5.getProperties());
              }, {}), i2 = at(t4, n2);
              this.queuedDeviceConfigEvents = [], this.isReady = false, this.configManager.updateConfigFromDeviceConfig(), this.configManager.resetLoadConfig(i2), null == (r2 = lr()) || r2.configure(), this.postMessage("load", [e4, i2.mediaType]), this.autoPlayOptions && this.postMessage("playIntent"), this.trySetupVideoTransformer();
            }, t3.updateLoadConfiguration = function(e4) {
              var t4 = at(e4);
              this.configManager.updateLoadConfig(t4);
            }, t3.play = function() {
              this.postMessage("playIntent"), this.mediaSinkManager.captureGesture(), this.paused = false, this.attemptPlay();
            }, t3.setAutoplay = function(e4) {
              this.autoPlayOptions = e4 ? { attemptMutedRetry: true } : null;
            }, t3.setAutoPlayOptions = function(e4) {
              this.autoPlayOptions = e4;
            }, t3.getExperiments = function() {
              return this.experiments;
            }, t3.setExperiment = function(e4, t4) {
              this.setExperimentData({ id: e4, assignment: t4, version: 0, type: "" });
            }, t3.setExperimentData = function(e4) {
              this.configManager.setExperiment(e4);
            }, t3.pause = function() {
              this.paused = true, this.postMessage("pause");
            }, t3.isPaused = function() {
              return this.paused;
            }, t3.seekTo = function(e4) {
              this.seekTime = e4, this.postMessage("seekTo", [e4]);
            }, t3.isSeeking = function() {
              return null !== this.seekTime;
            }, t3.isAutoplay = function() {
              return !!this.autoPlayOptions;
            }, t3.getDuration = function() {
              return this.state.duration;
            }, t3.getStartOffset = function() {
              return this.state.startOffset || 0;
            }, t3.getPosition = function() {
              return null === this.seekTime ? this.mediaSinkManager.getCurrentSink().getCurrentTime() : this.seekTime;
            }, t3.getSyncTime = function() {
              return this.state.syncTime;
            }, t3.getBuffered = function() {
              return this.mediaSinkManager.getCurrentSink().buffered();
            }, t3.getBufferedRanges = function() {
              return this.postMessage("updateBufferedRanges", []), this.state.trackBufferedRanges;
            }, t3.getSinkBufferedRanges = function() {
              var e4 = this.mediaSinkManager.getCurrentSink();
              return { audio: e4.getBufferedRanges("audio"), video: e4.getBufferedRanges("video") };
            }, t3.getBufferDuration = function() {
              return Math.max(0, this.state.bufferedPosition - this.getPosition());
            }, t3.getState = function() {
              return this.state.state;
            }, t3.getVideoWidth = function() {
              return this.getHTMLVideoElement().videoWidth;
            }, t3.getVideoHeight = function() {
              return this.getHTMLVideoElement().videoHeight;
            }, t3.getVideoFrameRate = function() {
              return this.state.statistics.framerate;
            }, t3.getVideoBitRate = function() {
              return this.state.statistics.bitrate;
            }, t3.getAverageBitrate = function() {
              return this.state.averageBitrate;
            }, t3.getBandwidthEstimate = function() {
              return this.state.bandwidthEstimate;
            }, t3.getPath = function() {
              return this.state.path;
            }, t3.getProtocol = function() {
              return this.state.protocol;
            }, t3.getVersion = function() {
              return "1.55.0";
            }, t3.isLiveLowLatency = function() {
              return this.state.liveLowLatencyEnabled && this.state.liveLowLatency;
            }, t3.isLooping = function() {
              return this.state.looping;
            }, t3.setLogLevel = function(e4) {
              this.configManager.setLogLevel(e4);
            }, t3.setLooping = function(e4) {
              this.state.looping = e4, this.postMessage("setLooping", [e4]);
            }, t3.isMuted = function() {
              return this.mediaSinkManager.getCurrentSink().isMuted();
            }, t3.setMuted = function(e4) {
              this.mediaSinkManager.getCurrentSink().setMuted(e4);
            }, t3.setVolume = function(e4) {
              this.state.volume = e4, this.postMessage("setVolume", [this.state.volume]);
            }, t3.skipAd = function() {
              this.postMessage("skipAd");
            }, t3.getVolume = function() {
              return this.state.volume;
            }, t3.getQuality = function() {
              return this.state.quality;
            }, t3.setQuality = function(e4, t4) {
              void 0 === t4 && (t4 = false), this.getHTMLVideoElement().controls || (this.postMessage("setQuality", [e4, t4]), this.state.autoQualityMode = false);
            }, t3.getQualities = function() {
              return this.state.qualities;
            }, t3.getUnavailableQualities = function() {
              return this.state.unavailableQualities;
            }, t3.setSourceGroup = function(e4, t4) {
              void 0 === t4 && (t4 = false), this.postMessage("setSourceGroup", [e4, t4]);
            }, t3.getSourceGroup = function() {
              return this.state.sourceGroup;
            }, t3.getSourceGroups = function() {
              return this.state.sourceGroups;
            }, t3.getTextTrack = function() {
              return this.state.textTrack;
            }, t3.getTextTracks = function() {
              return this.state.textTracks;
            }, t3.setTextTrack = function(e4) {
              this.postMessage("setTextTrack", [e4]);
            }, t3.setAuthToken = function(e4) {
              this.postMessage("setAuthToken", [e4]);
            }, t3.isAutoQualityMode = function() {
              return this.state.autoQualityMode;
            }, t3.setAutoQualityMode = function(e4) {
              this.state.autoQualityMode = e4, this.postMessage("setAutoQualityMode", [e4]);
            }, t3.setAutoInitialBitrate = function(e4) {
              this.postMessage("setAutoInitialBitrate", [e4]);
            }, t3.setAutoMaxQuality = function(e4) {
              this.postMessage("setAutoMaxQuality", [e4]);
            }, t3.setAutoMaxBitrate = function(e4) {
              this.postMessage("setAutoMaxBitrate", [e4]);
            }, t3.setAutoMaxVideoSize = function(e4, t4) {
              this.postMessage("setAutoMaxVideoSize", [e4, t4]);
            }, t3.setAutoViewportSize = function(e4, t4) {
              this.postMessage("setAutoViewportSize", [e4, t4]);
            }, t3.getPlaybackRate = function() {
              return this.mediaSinkManager.getCurrentSink().getPlaybackRate();
            }, t3.setPlaybackRate = function(e4) {
              return this.mediaSinkManager.getCurrentSink().setPlaybackRate(e4);
            }, t3.setClientId = function(e4) {
              this.postMessage("setClientId", [e4]);
            }, t3.setDeviceId = function(e4) {
              this.postMessage("setDeviceId", [e4]);
            }, t3.setLiveSpeedUpRate = function(e4) {
              this.postMessage("setLiveSpeedUpRate", [e4]);
            }, t3.setPlayerType = function(e4) {
              this.postMessage("setPlayerType", [e4]);
            }, t3.setLiveMaxLatency = function(e4) {
              this.postMessage("setLiveMaxLatency", [e4]);
            }, t3.setLiveLowLatencyEnabled = function(e4) {
              this.state.liveLowLatencyEnabled = e4, this.postMessage("setLiveLowLatencyEnabled", [e4]);
            }, t3.setRebufferToLive = function(e4) {
              var t4;
              null != (t4 = lr()) && null != (t4 = t4.getDriftDetectionConfig()) && t4.enabled || this.postMessage("setRebufferToLive", [e4]);
            }, t3.setVisible = function(e4) {
              this.postMessage("setVisible", [e4]);
            }, t3.setInitialBufferDuration = function(e4) {
              this.postMessage("setInitialBufferDuration", [e4]);
            }, t3.addEventListener = function(e4, t4) {
              this.emitter.on(e4, t4);
            }, t3.removeEventListener = function(e4, t4) {
              this.emitter.removeListener(e4, t4);
            }, t3.getDroppedFrames = function() {
              return this.state.statistics.droppedFrames;
            }, t3.getDecodedFrames = function() {
              return this.state.statistics.decodedFrames;
            }, t3.getDisplayWidth = function() {
              return this.renderSurface.displayDimensions().width;
            }, t3.getDisplayHeight = function() {
              return this.renderSurface.displayDimensions().height;
            }, t3.getSessionId = function() {
              return this.state.sessionId;
            }, t3.getChannelMetadata = function() {
              return this.state.channelMetadata;
            }, t3.getSessionData = function() {
              return this.state.sessionData;
            }, t3.getLiveLatency = function() {
              return this.state.liveLatency;
            }, t3.isProtected = function() {
              return this.mediaSinkManager.isProtected();
            }, t3.startRemotePlayback = function() {
              this.postMessage("startRemotePlayback");
            }, t3.endRemotePlayback = function() {
              this.postMessage("endRemotePlayback");
            }, t3.setPlatformName = function(e4) {
              this.postMessage("setPlatformName", [e4]);
            }, t3.setRequestCredentials = function(e4) {
              this.postMessage("setRequestCredentials", [e4]);
            }, t3.onSinkCreated = function(e4) {
              this.postMessage("onClientSinkCreated", [e4]);
            }, t3.onSinkTimeUpdate = function() {
              if (null === this.seekTime) {
                var e4 = this.mediaSinkManager.getCurrentSink(), t4 = this.renderSurface.stats(), r2 = t4.droppedFrames, n2 = t4.decodedFrames, i2 = this.renderSurface.displayDimensions(), o2 = i2.height, a2 = i2.width, s2 = e4.getGapSkipStatistics(), c2 = M(r2), l2 = M(n2), d2 = M(o2), f2 = M(a2);
                this.postMessage("onClientSinkUpdate", [{ currentTime: e4.getCurrentTime(), decodedFrames: l2, droppedFrames: c2, framerate: this.renderSurface.framerate(), bufferDuration: e4.bufferDuration(), displayHeight: d2, displayWidth: f2, gapSkipCount: s2.count, gapSkipDurationInSeconds: s2.durationInSeconds }]), this.setAutoViewportSize(a2 * window.devicePixelRatio, o2 * window.devicePixelRatio), this.emitter.emit(u.TIME_UPDATE, e4.getCurrentTime());
              }
            }, t3.onSinkBufferUpdate = function() {
              this.emitter.emit(u.BUFFER_UPDATE);
            }, t3.onSinkDurationChanged = function(e4) {
              this.postMessage("onClientSinkDurationChanged", [e4]);
            }, t3.onSinkEnded = function() {
              this.postMessage("onClientSinkEnded");
            }, t3.onSinkIdle = function() {
              this.postMessage("onClientSinkIdle");
            }, t3.onSinkBuffering = function() {
              this.postMessage("onClientSinkBuffering");
            }, t3.onSinkPlaying = function(e4) {
              this.postMessage("onClientSinkPlaying"), e4 && this.play();
            }, t3.onSinkStop = function(e4) {
              var t4, r2, n2 = A(document).hidden, i2 = this.configManager.getConfigSnapshot().features.allowBackgroundControl;
              if (i2 || !document[n2]) if (i2 && this.logger.debug('onSinkStop allowing background pause due to config "allowBackgroundControl" override'), e4) {
                if (!this.isMuted() && (null == (t4 = null == (r2 = this.autoPlayOptions) ? void 0 : r2.attemptMutedRetry) || t4)) return this.setMuted(true), this.mediaSinkManager.getCurrentSink().play(), void this.emitter.emit(u.AUDIO_BLOCKED);
                this.pause(), this.emitter.emit(u.PLAYBACK_BLOCKED);
              } else this.pause();
              else this.postMessage("pause");
            }, t3.onSinkReset = function() {
              this.postMessage("onClientSinkReset");
            }, t3.onSinkError = function(e4) {
              var t4 = e4.value, r2 = e4.code, n2 = e4.message;
              this.postMessage("onClientSinkError", [t4, r2, n2]);
            }, t3.onSinkRecoverableError = function(e4) {
              var t4 = e4.value, r2 = e4.code, n2 = e4.message;
              this.postMessage("onClientSinkRecoverableError", [t4, r2, n2]);
            }, t3.onSinkVolumeChanged = function(e4, t4) {
              this.getHTMLVideoElement().controls && t4 && this.setVolume(e4), this.emitter.emit(u.VOLUME_CHANGED, this.state.volume);
            }, t3.onSinkMutedChanged = function(e4) {
              this.postMessage("setMuted", [e4]), this.emitter.emit(u.MUTED_CHANGED);
            }, t3.onSinkPlaybackRateChanged = function(e4) {
              this.postMessage("setPlaybackRate", [e4]);
            }, t3.onPassthroughSinkDataCue = function(e4) {
              var t4;
              null == (t4 = this.daterangeTagAssembler) || t4.addCue(e4);
            }, t3.onPassthroughSinkMetadata = function(e4, t4, r2, n2, i2) {
              this.emitter.emit(u.TEXT_METADATA_CUE, { description: n2, endTime: t4, startTime: e4, text: r2, owner: i2, type: "TextMetadataCue" });
            }, t3.onPassthroughSinkPropertyChanged = function(e4, t4) {
              this.postMessage("onClientSinkPassthroughPropertyChanged", [e4, t4]);
            }, t3.onSinkControlsChanged = function(e4) {
              this.postMessage("setControls", [e4]);
            }, t3.onSinkGapJump = function(e4) {
              this.postMessage("onClientSinkGapJump", [e4]);
            }, t3.onRemoteDevice = function(e4) {
              this.emitter.emit(e4 ? Qt.AVAILABLE : Qt.UNAVAILABLE);
            }, t3.onRemoteReconnect = function() {
              this.startRemotePlayback();
            }, t3.onSessionError = function() {
              this.postMessage("onClientSinkError", [1, 0, "Chromecast session error"]);
            }, t3.onLoadMediaError = function() {
              this.postMessage("onClientSinkError", [1, 0, "Chromecast load media failed"]);
            }, t3.onUserCancel = function() {
              this.endRemotePlayback(), this.emitter.emit(Qt.SESSION_ENDED);
            }, t3.onSegmentDiscontinuity = function() {
              this.mediaSinkManager.onSegmentDiscontinuity();
            }, t3.onSessionStop = function() {
              this.endRemotePlayback(), this.emitter.emit(Qt.SESSION_ENDED);
            }, t3.onSessionStarted = function(e4) {
              this.emitter.emit(Qt.SESSION_STARTED, e4);
            }, e3.canDeviceConfigManagerRefreshNowCallback = function() {
              return e3.instanceCount > 0;
            }, e3.emitDeviceConfigManagerMetricsCallback = function(e4) {
              vt.getInstance().enqueue(e4);
            }, t3.emitDeviceConfigManagerMetrics = function() {
              for (var e4, t4 = di(this.deviceConfigMetricsHelper.dequeueAll()); !(e4 = t4()).done; ) {
                var r2 = e4.value;
                this.sendConfigManagerDeviceConfigAnalytics("ivs_devconf_ops_metrics", r2);
              }
            }, t3.sendConfigManagerDeviceConfigAnalytics = function(e4, t4) {
              if (this.isReady) {
                var r2 = w(t4);
                r2 && this.postMessage("onDeviceConfigAnalytics", [e4, r2]);
              } else this.queuedDeviceConfigEvents.push({ name: e4, properties: t4 });
            }, t3.flushQueuedDeviceConfigEvents = function() {
              for (var e4, t4 = di(this.queuedDeviceConfigEvents); !(e4 = t4()).done; ) {
                var r2 = e4.value, n2 = w(r2.properties);
                n2 && this.postMessage("onDeviceConfigAnalytics", [r2.name, n2]);
              }
              this.queuedDeviceConfigEvents = [];
            }, t3.attemptPlay = function() {
              var e4 = A(document).hidden, t4 = this.configManager.getConfigSnapshot().features.allowBackgroundControl;
              !this.isLoaded || !t4 && document[e4] || (t4 && this.logger.debug('attemptPlay allowing background play due to config "allowBackgroundControl" override'), this.postMessage("play"));
            }, t3.postMessage = function(e4, t4, r2) {
              void 0 === r2 && (r2 = []), this.worker.postMessage({ id: this.id, funcName: e4, args: t4 }, r2);
            }, t3.resetState = function() {
              Object.assign(this.state, bn()), this.emitter.emit(u.DURATION_CHANGED, 0), this.seekTime = null, this.isLoaded = false;
            }, t3.attachHandlers = function() {
              var e4 = this;
              this.worker.addEventListener("message", this.onmessage);
              var t4 = A(document).visibilityChange;
              document.addEventListener(t4, this.onvisibilitychange), window.addEventListener("online", this.onOnline), window.addEventListener("offline", this.onOffline);
              var r2 = this.emitter;
              r2.on(u.VOLUME_CHANGED, function() {
                return e4.onVolumeChanged();
              }), r2.on(u.MUTED_CHANGED, function() {
                return e4.onMutedChanged();
              }), r2.on(u.SEEK_COMPLETED, function() {
                return e4.onSeekCompleted();
              }), r2.on(u.ERROR, function() {
                return e4.onError();
              }), r2.on(u.SESSION_DATA, function(t5) {
                return e4.onSessionData(t5);
              }), r2.on(u.SEGMENT_DISCONTINUITY, function() {
                return e4.onSegmentDiscontinuity();
              }), r2.on(li.STATE_CHANGED, function(t5) {
                return e4.onStateChanged(t5);
              }), r2.on(li.MEDIA_SINK_RPC, function(t5) {
                try {
                  e4.mediaSinkManager.applyRPC(t5);
                } catch (t6) {
                  e4.onSinkError({ value: -1, code: -2, message: t6.message });
                }
              }), r2.on(li.CONFIGURE, function(t5) {
                var r3 = t5[0];
                try {
                  e4.mediaSinkManager.configure(r3);
                } catch (t6) {
                  e4.onSinkError({ value: -1, code: -3, message: t6.message });
                }
              }), r2.on(li.RESET, function() {
                try {
                  var t5, r3;
                  null == (t5 = e4.videoTransformerModule) || t5.stop(), null == (r3 = e4.videoTransformerModule) || r3.reset(), e4.mediaSinkManager.reset();
                } catch (t6) {
                  e4.onSinkRecoverableError({ value: -1, code: -4, message: t6.message });
                }
              }), r2.on(a.ID3, function(t5) {
                return e4.onID3(t5);
              }), r2.on(li.GET_EXPERIMENTS, function(t5) {
                e4.experiments = t5;
              }), r2.on(li.PROPERTY_CHANGED, function(t5) {
                var r3 = t5.key, n2 = t5.value;
                return e4.onCorePropertyChanged(r3, n2);
              }), r2.on(li.SYNC_TIME_CHANGED, function(t5) {
                e4.emitter.emit(u.SYNC_TIME_UPDATE, 1e3 * t5);
              }), r2.on(li.AD_BREAK_STARTED, function(t5) {
                e4.emitter.emit(u.AD_BREAK_STARTED, t5);
              }), r2.on(li.AD_CREATIVE_STARTED, function(t5) {
                e4.emitter.emit(u.AD_CREATIVE_STARTED, t5);
              }), r2.on(li.AD_TIME_UPDATE, function(t5) {
                e4.emitter.emit(u.AD_TIME_UPDATE, t5);
              }), r2.on(li.AD_CREATIVE_ENDED, function(t5) {
                e4.emitter.emit(u.AD_CREATIVE_ENDED, t5);
              }), r2.on(li.AD_BREAK_ENDED, function(t5) {
                e4.emitter.emit(u.AD_BREAK_ENDED, t5);
              }), r2.on(li.BUFFERED_RANGES, function(t5) {
                e4.state.trackBufferedRanges = t5;
              }), r2.on(li.LOG_MESSAGE, function(e5) {
                var t5 = e5.level, r3 = e5.message;
                return console[t5](r3);
              });
            }, t3.onCorePropertyChanged = function(e4, t4) {
              var r2;
              this.state[e4] = (function(e5, t5) {
                return "syncTime" === e5 && (t5 *= 1e3), t5;
              })(e4, t4), "quality" === e4 && (null == (r2 = this.videoTransformerModule) || r2.onQualityChanged(this.state[e4]));
            }, t3.onVolumeChanged = function() {
              var e4 = A(document).hidden;
              this.pauseHiddenSilentTab && document[e4] && 0 === this.getVolume() && this.postMessage("pause");
            }, t3.onMutedChanged = function() {
              var e4 = A(document).hidden;
              this.pauseHiddenSilentTab && document[e4] && this.isMuted() && this.postMessage("pause");
            }, t3.onSeekCompleted = function() {
              this.seekTime = null;
            }, t3.onError = function() {
              this.flushQueuedDeviceConfigEvents(), this.paused = true;
            }, t3.onStateChanged = function(e4) {
              var t4 = this;
              switch (e4) {
                case c.READY:
                  this.isReady = true, this.flushQueuedDeviceConfigEvents();
                  var r2 = this.isQualitySupported;
                  if (this.adjustments.abrTranscodesOnly && this.state.qualities.length > 1) {
                    var n2 = this.state.qualities.slice().sort(function(e5, t5) {
                      return t5.bitrate - e5.bitrate;
                    });
                    "chunked" !== n2[0].group ? r2 = function(e5) {
                      return "chunked" !== e5.group && t4.isQualitySupported(e5);
                    } : this.setAutoMaxQuality(n2[1]);
                  }
                  var i2 = (function(e5, t5) {
                    var r3 = [], n3 = [];
                    return e5.forEach(function(e6) {
                      t5(e6) ? r3.push(e6) : n3.push(e6);
                    }), { supported: r3, unsupported: n3 };
                  })(this.state.qualities, r2);
                  this.state.qualities = i2.supported, i2.unsupported.forEach(function(e5) {
                    return t4.removeQuality(e5);
                  }), this.isLoaded = true, this.autoPlayOptions && this.play(), this.paused || this.attemptPlay();
                  break;
                case c.IDLE:
                  var o2;
                  null == (o2 = this.videoTransformerModule) || o2.stop();
                  break;
                case c.PLAYING:
                  this.tryStartVideoTransformer();
                  break;
                case c.ENDED:
                  this.paused = true;
              }
              this.emitter.emit(u.STATE_CHANGED, e4), this.emitter.emit(e4);
            }, t3.removeQuality = function(e4) {
              this.postMessage("removeQuality", [e4]);
            }, t3.onID3 = function(e4) {
              var t4 = this;
              e4.forEach(function(e5) {
                if ("TXXX" === e5.id && "segmentmetadata" === e5.desc && e5.info.length) {
                  var r2 = k(e5.info[0]);
                  if (Object.prototype.hasOwnProperty.call(r2, "stream_offset")) {
                    var n2 = Number(r2.stream_offset);
                    isNaN(n2) || (t4.state.startOffset = n2 - t4.getPosition());
                  }
                }
              });
            }, t3.onVisibilityChange = function() {
              var e4, t4 = A(document).hidden, r2 = document[t4];
              this.paused || document[t4] || this.attemptPlay(), this.pauseHiddenSilentTab && !this.paused && r2 && (this.isMuted() || 0 === this.getVolume()) && this.postMessage("pause"), null == (e4 = this.videoTransformerModule) || e4.onPageVisibilityChanged(r2, this.getHTMLVideoElement()), this.postMessage("setVisible", [!r2]);
            }, t3.onSessionData = function(e4) {
              Object.assign(this.state, e4);
            }, t3.onConfigChanged = function() {
              var e4 = this.configManager.getConfigSnapshot();
              (0, K.setLogConfigByLevel)({ enabled: true, level: e4.logLevel }), (0, K.setLogConfigByCategories)(e4.logCategories), e4.features.gpu.flags.enable_render_surface && this.renderSurface.moveVideoToRenderSurface(), this.setupDaterangeTagAssembler(false), this.mediaSinkManager.onPlayerConfigurationChanged(e4), this.postMessage("updatePlayerConfiguration", [e4]);
            }, t3.setupDaterangeTagAssembler = function(e4) {
              var t4, r2, n2, i2, o2 = this;
              e4 ? this.daterangeTagAssembler || (this.daterangeTagAssembler = (t4 = function(e5) {
                var t5 = e5.attributes["X-TV-TWITCH-STREAM-SOURCE"];
                void 0 !== t5 && o2.emitter.emit(u.STREAM_SOURCE_CUE, { type: "StreamSourceCue", streamSource: t5 }), o2.emitter.emit(u.METADATA, { type: "text/json", data: w(f()({ ID: e5.ID, CLASS: e5.CLASS }, e5.attributes)) });
              }, r2 = /* @__PURE__ */ new Map(), n2 = function(e5, t5) {
                return Array.from(e5.attributes).every(function(e6) {
                  return void 0 !== t5.attributes[e6];
                });
              }, i2 = function(e5, r3, i3, o3) {
                for (var a2 = 0; a2 < r3.length; ++a2) {
                  var s2 = r3[a2];
                  if (s2.CLASS === e5.CLASS && void 0 === s2.attributes[i3]) return s2.attributes[i3] = o3, n2(e5, s2) && (r3.splice(a2, 1), t4(s2)), true;
                }
                return false;
              }, { addCue: function(e5) {
                var o3, a2, s2 = null == (o3 = e5.value) ? void 0 : o3.key, u2 = null == (a2 = e5.value) ? void 0 : a2.data;
                if (void 0 !== s2 && void 0 !== u2) for (var c2 = 0; c2 < yn.length; ++c2) {
                  var l2 = yn[c2];
                  if (l2.attributes.has(s2)) {
                    var d2 = e5.startTime + "-" + e5.endTime, f2 = r2.get(d2);
                    if (f2 || (f2 = [], r2.set(d2, f2)), !i2(l2, f2, s2, u2)) {
                      var h2, p2 = { ID: "com.apple.quicktime.FAKE-HLS-" + mn++, CLASS: l2.CLASS, attributes: (h2 = {}, h2[s2] = u2, h2.DURATION = "" + (e5.endTime - e5.startTime), h2["START-DATE"] = (/* @__PURE__ */ new Date()).toISOString(), h2) };
                      n2(l2, p2) ? t4(p2) : f2.push(p2);
                    }
                  }
                }
              } })) : this.daterangeTagAssembler = void 0;
            }, t3.trySetupVideoTransformer = function() {
              var e4 = this;
              if (this.videoTransformerModule) this.videoTransformerModule.onVideoSessionChanged();
              else {
                var t4 = this.configManager.getConfigSnapshot().features.gpu;
                if (t4.flags.init_transformer || t4.flags.run_transformer) {
                  var r2 = function(t5) {
                    e4.renderSurface.setActiveSurface(Jt.video), Be.warn("[MediaPlayer]: Video Transformer error, switching render surface to video", t5);
                  }, n2 = function(t5) {
                    Be.debug("[MediaPlayer]: Sending analytics event", t5), e4.postMessage(t5.name, [t5.data]);
                  };
                  this.videoTransformerModule = new ai(t4, { error: r2, analytics: n2, active: function() {
                    var r3, n3 = Jt.video;
                    t4.flags.allow_canvas_visible && (n3 = Jt.canvas, null != (r3 = t4.render) && r3.debug && (n3 = Jt.debug_both)), e4.renderSurface.setActiveSurface(n3);
                  }, inactive: function() {
                    e4.renderSurface.setActiveSurface(Jt.video);
                  }, syntheticQualities: function(t5) {
                    Be.info("Received synthetics", t5), e4.postMessage("setSyntheticQualities", [t5]);
                  } }), this.videoTransformerModule.init().then(function(r3) {
                    r3 && e4.videoTransformerModule && (t4.flags.add_canvas_to_surface && (Be.debug("[MediaPlayer]: Adding canvas to render surface"), e4.renderSurface.setCanvas(r3)), e4.getState() === c.PLAYING && e4.tryStartVideoTransformer());
                  }).catch(function(t5) {
                    var i2;
                    null == (i2 = e4.videoTransformerModule) || i2.disable(), Be.warn("[MediaPlayer]: Unhanled error initializing the Video Transformer");
                    var o2 = { source: "MediaPlayer", code: Tn.Init, message: "Unhandled error initializing the Video Transformer: " + ((null == t5 ? void 0 : t5.message) || "unknown"), fatal: true, gpu_architecture: "", gpu_description: "", gpu_device: "", gpu_vendor: "", surface_visible: false };
                    r2(), n2({ name: "onGpuError", data: o2 });
                  });
                }
              }
            }, t3.tryStartVideoTransformer = function() {
              this.videoTransformerModule && this.configManager.getConfigSnapshot().features.gpu.flags.run_transformer && this.videoTransformerModule.start(this.getHTMLVideoElement());
            }, t3.onWorkerMessage = function(e4) {
              var t4 = e4.data;
              if (t4 && t4.id === this.id) {
                var r2 = t4.type, n2 = t4.arg;
                void 0 !== t4.arg ? this.emitter.emit(r2, n2) : this.emitter.emit(r2);
              }
            }, t3.processVideoElementAttributes = function(e4) {
              if (e4.hasAttribute("autoplay") && (e4.removeAttribute("autoplay"), this.setAutoplay(true)), e4.hasAttribute("playbackRate")) {
                var t4, r2 = parseFloat(null != (t4 = e4.getAttribute("playbackRate")) ? t4 : "1.0");
                if (!isNaN(r2)) {
                  var n2 = I(r2, 0.25, 2);
                  this.setPlaybackRate(n2);
                }
                e4.removeAttribute("playbackRate");
              }
              if (e4.hasAttribute("src")) {
                var i2 = e4.src;
                D(e4), this.load(i2);
              }
              if (e4.hasAttribute("loop") && (e4.removeAttribute("loop"), this.setLooping(true)), e4.hasAttribute("muted") && (e4.removeAttribute("muted"), this.setMuted(true)), e4.hasAttribute("volume")) {
                var o2, a2 = parseFloat(null != (o2 = e4.getAttribute("volume")) ? o2 : "1.0");
                isNaN(a2) || this.setVolume(I(a2, 0, 1)), e4.removeAttribute("volume");
              }
            }, t3.checkDebugCaptureEnabled = function() {
            }, t3.getDeviceConfigPropertyHolder = function() {
              return this.configManager.getDeviceConfigPropertyHolder();
            }, e3;
          })();
          hi.instanceCount = 0, hi.instanceId = 0;
          var pi = (function() {
            function e3(e4) {
              var t4 = this;
              this.workerPort = void 0, this.emitter = void 0, this.messageQueue = void 0, this.workerPort = { postMessage: this.postMessageFromWorker.bind(this), onmessage: function() {
              } }, this.emitter = new er.EventEmitter(), this.messageQueue = new C(), this.loadScript(e4, function(e5) {
                return t4.applyWorkerEnv(e5);
              });
            }
            var t3 = e3.prototype;
            return t3.postMessage = function(e4) {
              this.messageQueue ? this.messageQueue.push(e4) : this.postMessageToWorker(e4);
            }, t3.addEventListener = function(e4, t4) {
              this.emitter.on(e4, t4);
            }, t3.removeEventListener = function(e4, t4) {
              this.emitter.off(e4, t4);
            }, t3.onmessage = function() {
            }, t3.onmessageerror = function() {
            }, t3.onerror = function() {
            }, t3.terminate = function() {
            }, t3.dispatchEvent = function() {
              return true;
            }, t3.loadScript = function(e4, t4) {
              var r2 = this, n2 = new XMLHttpRequest();
              n2.open("GET", e4), n2.addEventListener("load", function() {
                n2.status >= 200 && n2.status < 400 ? t4(n2.response) : r2.emitter.emit("error", new Error(n2.statusText));
              }), n2.addEventListener("error", function(e5) {
                r2.emitter.emit("error", e5);
              }), n2.send();
            }, t3.applyWorkerEnv = function(e4) {
              if (this.messageQueue) {
                try {
                  Function("self", "messageHandler", e4)(window, this.workerPort);
                } catch (e5) {
                  return void this.emitter.emit("error", e5);
                }
                for (; !this.messageQueue.empty(); ) this.postMessageToWorker(this.messageQueue.pop());
                this.messageQueue = null;
              }
            }, t3.postMessageFromWorker = function(e4) {
              var t4 = this;
              setTimeout(function() {
                t4.emitter.emit("message", { data: e4 });
              }, 0);
            }, t3.postMessageToWorker = function(e4) {
              var t4 = this;
              setTimeout(function() {
                t4.workerPort.onmessage({ data: e4 });
              }, 0);
            }, e3;
          })();
          function vi(e3, t3, r2) {
            var n2, i2, o2, a2;
            return void 0 === r2 && (r2 = false), xe().msIE ? n2 = new pi(e3) : (i2 = e3, o2 = window.location, (a2 = document.createElement("a")).href = i2, n2 = a2.hostname === o2.hostname && a2.port === o2.port && a2.protocol === o2.protocol ? new Worker(e3) : new Worker(URL.createObjectURL(new Blob(["importScripts('" + e3 + "')"])))), n2.postMessage({ wasmBinaryUrl: t3, showWorkerLogs: r2 }), n2;
          }
          var gi = "undefined" != typeof window && "object" == typeof window.WebAssembly && "function" == typeof window.WebAssembly.instantiate, mi = gi;
          function yi(e3) {
            var t3 = e3.asmWorker, r2 = e3.wasmWorker, n2 = e3.wasmBinary;
            if (!mi && !t3) throw new Error("WebAssembly is not supported by the browser. This is required for playback.");
            var i2 = vi(mi ? r2 : t3, n2, e3.showWorkerLog);
            return new Si(e3, i2);
          }
          function bi() {
            return "1.55.0";
          }
          var Ei, Si = (function() {
            function e3(e4, t4) {
              this.core = void 0, this.startCapture = void 0, this.stopCapture = void 0, this.requestCaptureAnalytics = void 0, this.core = new hi(e4, t4);
            }
            var t3 = e3.prototype;
            return t3.addEventListener = function(e4, t4) {
              var r2;
              null == (r2 = this.checkCore()) || r2.addEventListener(e4, t4);
            }, t3.attachHTMLVideoElement = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.attachHTMLVideoElement(e4);
            }, t3.delete = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.delete(), this.core = null;
            }, t3.endRemotePlayback = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.endRemotePlayback();
            }, t3.isAutoplay = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isAutoplay();
            }, t3.isAutoQualityMode = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isAutoQualityMode();
            }, t3.getAverageBitrate = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getAverageBitrate();
            }, t3.getBandwidthEstimate = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getBandwidthEstimate();
            }, t3.getBufferDuration = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getBufferDuration();
            }, t3.getBuffered = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getBuffered();
            }, t3.getBufferedRanges = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getBufferedRanges();
            }, t3.getSinkBufferedRanges = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSinkBufferedRanges();
            }, t3.getDecodedFrames = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getDecodedFrames();
            }, t3.getDisplayHeight = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getDisplayHeight();
            }, t3.getDisplayWidth = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getDisplayWidth();
            }, t3.getDroppedFrames = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getDroppedFrames();
            }, t3.getDuration = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getDuration();
            }, t3.getExperiments = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getExperiments();
            }, t3.getHTMLVideoElement = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getHTMLVideoElement();
            }, t3.getVideoRenderSurface = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVideoRenderSurface();
            }, t3.getLiveLatency = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getLiveLatency();
            }, t3.getPath = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getPath();
            }, t3.getProtocol = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getProtocol();
            }, t3.getPlaybackRate = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getPlaybackRate();
            }, t3.getPosition = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getPosition();
            }, t3.getSyncTime = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSyncTime();
            }, t3.getQualities = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getQualities();
            }, t3.getUnavailableQualities = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getUnavailableQualities();
            }, t3.getQuality = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getQuality();
            }, t3.getSessionData = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSessionData();
            }, t3.getSessionId = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSessionId();
            }, t3.getChannelMetadata = function() {
              var e4, t4;
              return null != (e4 = null == (t4 = this.checkCore()) ? void 0 : t4.getChannelMetadata()) ? e4 : [];
            }, t3.getStartOffset = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getStartOffset();
            }, t3.getState = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getState();
            }, t3.getVersion = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVersion();
            }, t3.getVideoBitRate = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVideoBitRate();
            }, t3.getVideoFrameRate = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVideoFrameRate();
            }, t3.getVideoHeight = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVideoHeight();
            }, t3.getVideoWidth = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVideoWidth();
            }, t3.getVolume = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getVolume();
            }, t3.isLiveLowLatency = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isLiveLowLatency();
            }, t3.isLooping = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isLooping();
            }, t3.isMuted = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isMuted();
            }, t3.isPaused = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isPaused();
            }, t3.isProtected = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isProtected();
            }, t3.isSeeking = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.isSeeking();
            }, t3.load = function(e4, t4) {
              var r2;
              return null == (r2 = this.checkCore()) ? void 0 : r2.load(e4, t4);
            }, t3.updateLoadConfiguration = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.updateLoadConfiguration(e4);
            }, t3.pause = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.pause();
            }, t3.play = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.play();
            }, t3.removeEventListener = function(e4, t4) {
              var r2;
              null == (r2 = this.checkCore()) || r2.removeEventListener(e4, t4);
            }, t3.seekTo = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.seekTo(e4);
            }, t3.setAuthToken = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAuthToken(e4);
            }, t3.setAutoInitialBitrate = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoInitialBitrate(e4);
            }, t3.setAutoMaxQuality = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoMaxQuality(e4);
            }, t3.setAutoMaxBitrate = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoMaxBitrate(e4);
            }, t3.setAutoMaxVideoSize = function(e4, t4) {
              var r2;
              null == (r2 = this.checkCore()) || r2.setAutoMaxVideoSize(e4, t4);
            }, t3.setAutoplay = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoplay(e4);
            }, t3.setAutoPlayOptions = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoPlayOptions(e4);
            }, t3.setAutoQualityMode = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setAutoQualityMode(e4);
            }, t3.setAutoViewportSize = function(e4, t4) {
              var r2;
              null == (r2 = this.checkCore()) || r2.setAutoViewportSize(e4, t4);
            }, t3.setClientId = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setClientId(e4);
            }, t3.setDeviceId = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setDeviceId(e4);
            }, t3.setExperiment = function(e4, t4) {
              var r2;
              null == (r2 = this.checkCore()) || r2.setExperiment(e4, t4);
            }, t3.setExperimentData = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setExperimentData(e4);
            }, t3.setInitialBufferDuration = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setInitialBufferDuration(e4);
            }, t3.setLiveLowLatencyEnabled = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setLiveLowLatencyEnabled(e4);
            }, t3.setLiveMaxLatency = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setLiveMaxLatency(e4);
            }, t3.setLiveSpeedUpRate = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setLiveSpeedUpRate(e4);
            }, t3.setLogLevel = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setLogLevel(e4);
            }, t3.setLooping = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setLooping(e4);
            }, t3.setMuted = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setMuted(e4);
            }, t3.setPlaybackRate = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setPlaybackRate(e4);
            }, t3.setPlayerType = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setPlayerType(e4);
            }, t3.setQuality = function(e4, t4) {
              var r2;
              void 0 === t4 && (t4 = false), null == (r2 = this.checkCore()) || r2.setQuality(e4, t4);
            }, t3.setSourceGroup = function(e4, t4) {
              var r2;
              void 0 === t4 && (t4 = false), null == (r2 = this.checkCore()) || r2.setSourceGroup(e4, t4);
            }, t3.getSourceGroup = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSourceGroup();
            }, t3.getSourceGroups = function() {
              var e4;
              return null == (e4 = this.checkCore()) ? void 0 : e4.getSourceGroups();
            }, t3.getTextTrack = function() {
              var e4, t4;
              return null != (e4 = null == (t4 = this.checkCore()) ? void 0 : t4.getTextTrack()) ? e4 : null;
            }, t3.getTextTracks = function() {
              var e4, t4;
              return null != (e4 = null == (t4 = this.checkCore()) ? void 0 : t4.getTextTracks()) ? e4 : [];
            }, t3.setTextTrack = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setTextTrack(e4);
            }, t3.setRebufferToLive = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setRebufferToLive(e4);
            }, t3.setVisible = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setVisible(e4);
            }, t3.setVolume = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setVolume(e4);
            }, t3.startRemotePlayback = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.startRemotePlayback();
            }, t3.setPlatformName = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setPlatformName(e4);
            }, t3.setRequestCredentials = function(e4) {
              var t4;
              null == (t4 = this.checkCore()) || t4.setRequestCredentials(e4);
            }, t3.checkCore = function() {
              return this.core || console.warn("Method called on deleted player instance."), this.core;
            }, t3.skipAd = function() {
              var e4;
              null == (e4 = this.checkCore()) || e4.skipAd();
            }, e3;
          })(), Ti = { PREROLL: "preroll", MIDROLL: "midroll", POSTROLL: "postroll", UNKNOWN: "unknown" }, _i = (function(e3) {
            return e3.DURATION_CHANGE = "durationchange", e3.ENDED = "ended", e3.ERROR = "error", e3.LOADED_METADATA = "loadedmetadata", e3.LOADSTART = "loadstart", e3.PAUSE = "pause", e3.PLAY = "play", e3.PLAYING = "playing", e3.RATE_CHANGE = "ratechange", e3.SEEKED = "seeked", e3.SEEKING = "seeking", e3.TIME_UPDATE = "timeupdate", e3.VOLUME_CHANGE = "volumechange", e3.WAITING = "waiting", e3;
          })({}), Ci = (function(e3) {
            return e3[e3.HAVE_NOTHING = 0] = "HAVE_NOTHING", e3[e3.HAVE_METADATA = 1] = "HAVE_METADATA", e3[e3.HAVE_CURRENT_DATA = 2] = "HAVE_CURRENT_DATA", e3[e3.HAVE_FUTURE_DATA = 3] = "HAVE_FUTURE_DATA", e3[e3.HAVE_ENOUGH_DATA = 4] = "HAVE_ENOUGH_DATA", e3;
          })(Ci || {}), ki = ((Ei = {})[c.IDLE] = 1, Ei[c.READY] = 1, Ei[c.BUFFERING] = 2, Ei[c.PLAYING] = 2, Ei[c.ENDED] = 1, Ei), wi = "AmazonIVS";
          function Pi(e3, r2) {
            if (void 0 === e3 || "function" != typeof e3.getTech) throw { message: "videojs not available, AmazonIVS tech not registered", code: 1 };
            if (!mi) throw { message: "WebAssembly support is required for AmazonIVS tech", code: 2 };
            if (!e3.getTech(wi)) {
              var n2, i2 = { featuresProgressEvents: true, featuresTimeupdateEvents: true, featuresPlaybackRate: true, featuresFullscreenResize: true, featuresVolumeControl: true, featuresMuteControl: true, featuresNativeTextTracks: false, privateContructor: function(t3, n3, i3) {
                this._readyState = Ci.HAVE_NOTHING, this._defaultPlaybackRate = 1, this._seeking = false, r2.playerFramework = { name: "videojs", version: e3.VERSION }, this._mediaPlayer = yi(r2), this._mediaPlayer.setAutoplay(true === t3.autoplay), this._attachVideojsListeners(), this._mediaPlayer.addEventListener(u.METADATA, this._onCaptionEvent.bind(this));
                var o3 = this._createEl(t3);
                t3.el = o3, i3(this, t3, n3), window.vttjs && window.vttjs.restore(), this.triggerReady(), setTimeout(function() {
                  var e4 = this.options(), t4 = e4.loop, r3 = e4.muted;
                  t4 && this._mediaPlayer.setLooping(true), r3 && this._mediaPlayer.setMuted(true);
                }.bind(this), 0);
              }, dispose: function() {
                this._mediaPlayer.delete();
              }, setPreload: function() {
              }, autoplay: function(e4) {
                if ("boolean" != typeof e4) return this._mediaPlayer.autoplay;
                this.setAutoplay(e4);
              }, setAutoplay: function(e4) {
                this._mediaPlayer.setAutoplay(e4);
              }, preload: function() {
              }, load: function() {
              }, readyState: function() {
                return this._readyState;
              }, seeking: function() {
                return this._seeking;
              }, networkState: function() {
                if (!this._mediaPlayer) return 0;
                var e4 = this._mediaPlayer.getHTMLVideoElement();
                if (!e4.src && !e4.srcObject) return 3;
                var t3 = this._mediaPlayer.getState();
                return ki[t3];
              }, ended: function() {
                return this._mediaPlayer.getState() === c.ENDED;
              }, seekable: function() {
                return e3.createTimeRange(0, this._mediaPlayer.getDuration());
              }, play: function() {
                this._mediaPlayer.play(), this.trigger(_i.PLAY);
              }, pause: function() {
                this._mediaPlayer.pause();
              }, setCurrentTime: function(e4) {
                var t3 = this._mediaPlayer.getHTMLVideoElement();
                (t3.src || t3.srcObject) && (this._mediaPlayer.seekTo(e4), this._seeking = true, this.trigger(_i.SEEKING));
              }, controls: function() {
                return false;
              }, setControls: function() {
                return false;
              }, muted: function() {
                return this._mediaPlayer.isMuted();
              }, setMuted: function(e4) {
                this._mediaPlayer.setMuted(e4);
              }, volume: function() {
                return this._mediaPlayer.getVolume();
              }, setVolume: function(e4) {
                this._mediaPlayer.setVolume(e4);
              }, defaultPlaybackRate: function(e4) {
                if (!e4) return this._defaultPlaybackRate;
                this._defaultPlaybackRate = e4;
              }, playbackRate: function() {
                return this._mediaPlayer.getPlaybackRate();
              }, setPlaybackRate: function(e4) {
                this._mediaPlayer.setPlaybackRate(e4);
              }, paused: function() {
                return this._mediaPlayer.isPaused();
              }, duration: function() {
                return this._mediaPlayer.getDuration();
              }, currentTime: function() {
                return this._mediaPlayer.getPosition();
              }, _createEl: function(e4) {
                var t3 = this._mediaPlayer.getHTMLVideoElement();
                t3.setAttribute("class", "vjs-tech"), void 0 !== e4.disablePictureInPicture && (t3.disablePictureInPicture = e4.disablePictureInPicture), ["preload", "poster"].forEach(function(r4) {
                  e4[r4] && t3.setAttribute(r4, e4[r4]);
                }.bind(this)), e4.playsinline && (t3.setAttribute("webkit-playsinline", ""), t3.setAttribute("playsinline", ""));
                var r3 = document.createElement("div");
                return r3.appendChild(t3), r3;
              }, src: function(e4) {
                this.trigger(_i.LOADSTART), this._seeking = false, this._captionTrack && (this.textTracks().removeTrack(this._captionTrack), this._captionTrack = null), e4 && this._mediaPlayer.load(e4);
              }, addEventListener: function(e4, t3) {
                this._mediaPlayer.addEventListener(e4, t3);
              }, removeEventListener: function(e4, t3) {
                this._mediaPlayer.removeEventListener(e4, t3);
              }, getMediaPlayerAPI: function() {
                return this._mediaPlayer;
              }, supportsFullScreen: function() {
                return true;
              }, enterFullScreen: function() {
                var e4 = this._mediaPlayer.getHTMLVideoElement();
                (e4.requestFullscreen || e4.webkitRequestFullscreen || e4.mozRequestFullScreen || e4.msRequestFullscreen || e4.webkitEnterFullscreen || function() {
                  console.error("Fullscreen API is not available");
                }).call(e4);
              }, exitFullScreen: function() {
                (document.exitFullScreen || document.webkitExitFullscreen || document.mozCancelFullScreen || document.msExitFullscreen || function() {
                  console.error("Exitscreen API is not available");
                }).call(document);
              }, requestPictureInPicture: function() {
                return this._mediaPlayer.getHTMLVideoElement().requestPictureInPicture();
              }, setDisablePictureInPicture: function(e4) {
                this._mediaPlayer.getHTMLVideoElement().disablePictureInPicture = e4;
              }, disablePictureInPicture: function() {
                return this._mediaPlayer.getHTMLVideoElement().disablePictureInPicture;
              }, _onCaptionEvent: function(e4) {
                if ("text/json" === e4.type) {
                  var t3 = JSON.parse(e4.data);
                  if (t3.caption) {
                    var r3 = t3.caption;
                    this._captionTrack || (this._captionTrack = this.addTextTrack("captions", r3.format), this._currentCue = null), this._currentCue && this._captionTrack.removeCue(this._currentCue);
                    var n3 = this._mediaPlayer.getHTMLVideoElement(), i3 = window.VTTCue || window.vttjs.VTTCue;
                    i3 ? (this._currentCue = new i3(n3.currentTime, n3.currentTime + 2, r3.text), this._captionTrack.addCue(this._currentCue)) : console.warn("No VTTCue implementation available, caption may not be available");
                  }
                }
              }, _attachVideojsListeners: function() {
                this._mediaPlayer.addEventListener(c.READY, function() {
                  this._readyState = Ci.HAVE_METADATA, this.trigger(_i.LOADED_METADATA);
                }.bind(this)), this._mediaPlayer.addEventListener(c.IDLE, function() {
                  this._readyState = Ci.HAVE_NOTHING, this.trigger(_i.PAUSE);
                }.bind(this)), this._mediaPlayer.addEventListener(c.PLAYING, function() {
                  this._readyState <= Ci.HAVE_CURRENT_DATA && (this._readyState = Ci.HAVE_FUTURE_DATA), this.trigger(_i.PLAY), this.trigger(_i.PLAYING);
                }.bind(this)), this._mediaPlayer.addEventListener(c.ENDED, function() {
                  this._readyState = Ci.HAVE_NOTHING, this.trigger(_i.ENDED);
                }.bind(this)), this._mediaPlayer.addEventListener(c.BUFFERING, function() {
                  this._readyState = Ci.HAVE_CURRENT_DATA;
                }.bind(this)), this._mediaPlayer.addEventListener(u.REBUFFERING, function() {
                  this._readyState = Ci.HAVE_CURRENT_DATA, this.trigger(_i.WAITING);
                }.bind(this)), this._mediaPlayer.addEventListener(u.TIME_UPDATE, function() {
                  this.trigger(_i.TIME_UPDATE);
                }.bind(this)), this._mediaPlayer.addEventListener(u.VOLUME_CHANGED, function() {
                  this.trigger(_i.VOLUME_CHANGE);
                }.bind(this)), this._mediaPlayer.addEventListener(u.MUTED_CHANGED, function() {
                  this.trigger(_i.VOLUME_CHANGE);
                }.bind(this)), this._mediaPlayer.addEventListener(u.ERROR, function() {
                  this.trigger(_i.ERROR);
                }.bind(this)), this._mediaPlayer.addEventListener(u.DURATION_CHANGED, function() {
                  this.trigger(_i.DURATION_CHANGE);
                }.bind(this)), this._mediaPlayer.addEventListener(u.SEEK_COMPLETED, function() {
                  this._seeking = false, this.trigger(_i.SEEKED);
                }.bind(this)), this._mediaPlayer.addEventListener(u.PLAYBACK_RATE_CHANGED, function() {
                  this.trigger(_i.RATE_CHANGE);
                }.bind(this));
              } }, o2 = e3.getTech("Tech");
              "function" == typeof e3.extend ? (i2.constructor = function(e4, t3) {
                this.privateContructor(e4, t3, function(e5, t4, r3) {
                  o2.call(e5, t4, r3);
                });
              }, n2 = e3.extend(o2, i2, wi)) : ((n2 = function(e4, t3) {
                this.privateContructor(e4, t3, function(e5, t4, r3) {
                  var n3 = o2.prototype, i3 = n3.featuresProgressEvents, a2 = n3.featuresTimeupdateEvents, s3 = n3.featuresPlaybackRate, u2 = n3.featuresFullscreenResize, c2 = n3.featuresVolumeControl, l2 = n3.featuresMuteControl, d2 = n3.featuresNativeTextTracks;
                  n3.featuresProgressEvents = true, n3.featuresTimeupdateEvents = true, n3.featuresPlaybackRate = true, n3.featuresFullscreenResize = true, n3.featuresVolumeControl = true, n3.featuresMuteControl = true, n3.featuresNativeTextTracks = false, Object.assign(e5, new o2(t4, r3)), e5.on(_i.PLAYING, function() {
                    e5.hasStarted_ = true;
                  }), e5.on(_i.LOADSTART, function() {
                    e5.hasStarted_ = false;
                  }), n3.featuresProgressEvents = i3, n3.featuresTimeupdateEvents = a2, n3.featuresPlaybackRate = s3, n3.featuresFullscreenResize = u2, n3.featuresVolumeControl = c2, n3.featuresMuteControl = l2, n3.featuresNativeTextTracks = d2;
                });
              }).prototype = Object.create(o2.prototype), Object.assign(n2.prototype, i2)), n2.supportsFullScreen = function() {
                return true;
              }, n2.isSupported = function() {
                return -1 === (navigator.appVersion || "").toLowerCase().indexOf("rv:11");
              }, n2.canPlayType = function(e4) {
                return "string" == typeof e4 && e4.length > 0 && (e4.indexOf("application/x-mpegURL") > -1 ? "undefined" != typeof MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E,mp4a.40.2"') : "" !== document.createElement("video").canPlayType(e4));
              }, n2.canPlaySource = function() {
                return true;
              }, e3.registerTech("AmazonIVS", n2);
              var s2 = e3.registerPlugin || e3.plugin;
              s2("getIVSEvents", function() {
                return { PlayerEventType: u, MetadataEventType: a, PlayerState: c, ErrorType: t2 };
              }), s2("getIVSPlayer", function() {
                return this.tech(true).getMediaPlayerAPI();
              });
            }
          }
          var Ai = "enableIVSQualityPlugin";
          function Ii(e3) {
            if (void 0 === e3 || "function" != typeof e3.getTech) throw { message: "videojs not available, Amazon IVS Quality Plugin not registered", code: 1 };
            if (!e3.getPlugin(Ai)) {
              var t3, r2 = e3.getComponent("MenuButton"), n2 = e3.getComponent("MenuItem"), i2 = { privateConstructor: function(e4, t4, r3) {
                r3(this, e4, t4), this.controlText("Quality");
              }, createItems: function() {
                var e4 = this.player(), t4 = e4.getIVSPlayer(), r3 = [], i3 = new n2(e4, { selectable: true, label: "Auto", qualityGroup: "auto" });
                i3.selected(t4.isAutoQualityMode()), i3.on(["click", "tap"], function() {
                  t4.setAutoQualityMode(true);
                }), i3.on(["click", "tap"], this._clickHandler.bind(this, i3)), r3.push(i3);
                var o2 = t4.getQuality(), a2 = t4.getQualities();
                return a2 && a2.length > 0 && a2.forEach(function(i4) {
                  var a3 = new n2(e4, { selectable: true, label: i4.name, qualityGroup: i4.group }), s2 = function(e5) {
                    t4.setQuality(e5);
                  }.bind(null, i4);
                  a3.on(["click", "tap"], s2), a3.on(["click", "tap"], this._clickHandler.bind(this, a3)), t4.isAutoQualityMode() || a3.selected(o2.group === i4.group), r3.push(a3);
                }.bind(this)), t4.addEventListener(u.QUALITY_CHANGED, function(r4) {
                  var n3;
                  if (!t4.isAutoQualityMode()) {
                    var i4 = null == (n3 = e4.controlBar) ? void 0 : n3.getChild("QualityMenuButton");
                    null == i4 || i4.items.forEach(function(e5) {
                      var t5 = e5.options().qualityGroup;
                      r4 && t5 && e5.selected(r4.group === t5);
                    });
                  }
                }), this.qualityItems = r3, r3;
              }, buildCSSClass: function() {
                return "vjs-icon-hd vjs-icon-placeholder " + r2.prototype.buildCSSClass.call(this);
              }, _clickHandler: function(e4) {
                this.items.forEach(function(t4) {
                  t4 !== e4 && t4.selected(false);
                });
              } };
              "function" == typeof e3.extend ? (i2.constructor = function(e4, t4) {
                this.privateConstructor(e4, t4, function(e5, t5, n3) {
                  r2.call(e5, t5, n3);
                });
              }, t3 = e3.extend(r2, i2)) : ((t3 = function(e4, t4) {
                this.privateConstructor(e4, t4, function(e5, t5, n3) {
                  var i3 = "quality-control";
                  n3.name = i3;
                  var o2 = new r2(t5, n3);
                  o2.getChild(i3).addClass("vjs-icon-hd vjs-icon-placeholder"), Object.assign(e5, o2), e5.items = e5.createItems(), e5.items.forEach(function(t6) {
                    e5.menu.addItem(t6);
                  });
                });
              }).prototype = Object.create(r2.prototype), Object.assign(t3.prototype, i2)), e3.registerComponent("QualityMenuButton", t3), (e3.registerPlugin || e3.plugin)(Ai, function() {
                var e4 = this;
                e4.getIVSPlayer().addEventListener(c.READY, function() {
                  var t4, r3, n3 = null == (t4 = e4.controlBar) ? void 0 : t4.getChild("QualityMenuButton");
                  n3 && (n3.dispose(), e4.controlBar.removeChild(n3)), null == (r3 = e4.controlBar) || r3.addChild("QualityMenuButton");
                });
              });
            }
          }
        })(), module.exports = n;
      })();
    }
  });
  return require_dist();
})();
/*! Bundled license information:

@babel/runtime/helpers/regenerator.js:
  (*! regenerator-runtime -- Copyright (c) 2014-present, Facebook, Inc. -- license (MIT): https://github.com/babel/babel/blob/main/packages/babel-helpers/LICENSE *)

amazon-ivs-player/dist/index.js:
  (*! For license information please see index.js.LICENSE.txt *)
*/
