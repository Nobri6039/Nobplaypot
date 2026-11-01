// Nobplaypot.cpp — Nobplaypot 启动器
//
// 职责(只做四件事):
//   1. 读取 Bin\launch.json, 决定用什么 runtime、跑哪个脚本、工作目录、参数、环境变量;
//   2. 校验 Bin\payload.manifest 的 RSA 签名, 再逐个校验清单里的文件 SHA-256;
//   3. 直接用 CreateProcessW 启动 pythonw(不经过 cmd/bat, 无控制台黑窗口);
//   4. 出错时写 Bin\launch.log 并弹 MessageBox。
//
// 更新 py / python-runtime / vlc-runtime 后, 运行 tools\sign.ps1 重新生成
// payload.manifest 与 payload.sig 即可, exe 本身不用换。

#include <windows.h>
#include <bcrypt.h>
#include <shellapi.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <cwctype>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <string>
#include <vector>

#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")

#include "pubkey.h"

namespace fs = std::filesystem;

// =====================================================================
// 基础工具
// =====================================================================
static std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

static std::string WideToUtf8(const std::wstring& s) {
    if (s.empty()) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string a(static_cast<size_t>(n), '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), static_cast<int>(s.size()),
                        a.data(), n, nullptr, nullptr);
    return a;
}

static std::wstring GetExeDir() {
    std::vector<wchar_t> buf(512);
    for (;;) {
        DWORD n = GetModuleFileNameW(nullptr, buf.data(), static_cast<DWORD>(buf.size()));
        if (n == 0) return {};
        if (n < buf.size() - 1) {
            buf.resize(n);
            break;
        }
        buf.resize(buf.size() * 2);
    }
    return fs::path(std::wstring(buf.begin(), buf.end())).parent_path().wstring();
}

static bool ReadFileBytes(const fs::path& p, std::vector<unsigned char>& out) {
    std::ifstream f(p, std::ios::binary | std::ios::ate);
    if (!f) return false;
    std::streamsize n = f.tellg();
    if (n < 0) return false;
    out.resize(static_cast<size_t>(n));
    f.seekg(0);
    if (n > 0) {
        f.read(reinterpret_cast<char*>(out.data()), n);
        if (!f) return false;
    }
    return true;
}

static bool ReadFileText(const fs::path& p, std::string& out) {
    std::ifstream f(p, std::ios::binary);
    if (!f) return false;
    out.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    return true;
}

static std::string TrimAscii(const std::string& s) {
    size_t b = 0, e = s.size();
    while (b < e && (s[b] == ' ' || s[b] == '\t' || s[b] == '\r' || s[b] == '\n'))
        ++b;
    while (e > b && (s[e - 1] == ' ' || s[e - 1] == '\t' || s[e - 1] == '\r' || s[e - 1] == '\n'))
        --e;
    return s.substr(b, e - b);
}

static void StripUtf8Bom(std::string& s) {
    if (s.size() >= 3 && static_cast<unsigned char>(s[0]) == 0xEF
        && static_cast<unsigned char>(s[1]) == 0xBB
        && static_cast<unsigned char>(s[2]) == 0xBF) {
        s.erase(0, 3);
    }
}

// =====================================================================
// 日志
// =====================================================================
class LauncherLog {
public:
    explicit LauncherLog(const fs::path& p) : path_(p) {
        std::error_code ec;
        if (!path_.parent_path().empty()) fs::create_directories(path_.parent_path(), ec);
    }

    void Write(const std::wstring& line) {
        std::ofstream f(path_, std::ios::app | std::ios::binary);
        if (!f) return;
        SYSTEMTIME st{};
        GetLocalTime(&st);
        char ts[64] = {};
        std::snprintf(ts, sizeof(ts), "%04d-%02d-%02d %02d:%02d:%02d ",
                      st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
        f << ts << WideToUtf8(line) << "\r\n";
    }

private:
    fs::path path_;
};

static int FailWith(LauncherLog& log, const std::wstring& message) {
    log.Write(L"错误: " + message);
    MessageBoxW(nullptr, message.c_str(), L"Nobplaypot", MB_OK | MB_ICONERROR);
    return 1;
}

// =====================================================================
// 哈希与签名
// =====================================================================
static bool Sha256Raw(const std::vector<unsigned char>& data, unsigned char out[32],
                      std::wstring& err) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    NTSTATUS st = BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0);
    if (!BCRYPT_SUCCESS(st)) {
        err = L"SHA-256 初始化失败";
        return false;
    }
    st = BCryptHash(alg, nullptr, 0,
                    data.empty() ? nullptr : const_cast<PUCHAR>(data.data()),
                    static_cast<ULONG>(data.size()), out, 32);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (!BCRYPT_SUCCESS(st)) {
        err = L"SHA-256 计算失败";
        return false;
    }
    return true;
}

static std::wstring HexLower(const unsigned char* data, size_t n) {
    static const wchar_t* digits = L"0123456789abcdef";
    std::wstring out;
    out.reserve(n * 2);
    for (size_t i = 0; i < n; ++i) {
        out.push_back(digits[data[i] >> 4]);
        out.push_back(digits[data[i] & 0x0F]);
    }
    return out;
}

static bool Base64Decode(const std::string& in, std::vector<unsigned char>& out) {
    static const std::string table =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        if (c == '=') break;
        if (c == ' ' || c == '\r' || c == '\n' || c == '\t') continue;
        size_t p = table.find(static_cast<char>(c));
        if (p == std::string::npos) return false;
        val = (val << 6) + static_cast<int>(p);
        bits += 6;
        if (bits >= 0) {
            out.push_back(static_cast<unsigned char>((val >> bits) & 0xFF));
            bits -= 8;
        }
    }
    return !out.empty();
}

static bool VerifyRsaSignature(const std::vector<unsigned char>& data,
                               const std::vector<unsigned char>& signature,
                               std::wstring& err) {
    unsigned char hash[32] = {};
    if (!Sha256Raw(data, hash, err)) return false;

    BCRYPT_ALG_HANDLE alg = nullptr;
    if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_RSA_ALGORITHM, nullptr, 0))) {
        err = L"RSA 初始化失败";
        return false;
    }
    BCRYPT_KEY_HANDLE key = nullptr;
    NTSTATUS st = BCryptImportKeyPair(alg, nullptr, BCRYPT_RSAPUBLIC_BLOB, &key,
                                      const_cast<PUCHAR>(kPublicKey),
                                      kPublicKeySize, 0);
    if (!BCRYPT_SUCCESS(st)) {
        BCryptCloseAlgorithmProvider(alg, 0);
        err = L"内置公钥无效";
        return false;
    }
    BCRYPT_PKCS1_PADDING_INFO padding{};
    padding.pszAlgId = BCRYPT_SHA256_ALGORITHM;
    st = BCryptVerifySignature(key, &padding, hash, 32,
                               const_cast<PUCHAR>(signature.data()),
                               static_cast<ULONG>(signature.size()),
                               BCRYPT_PAD_PKCS1);
    BCryptDestroyKey(key);
    BCryptCloseAlgorithmProvider(alg, 0);
    if (!BCRYPT_SUCCESS(st)) {
        err = L"签名校验失败: payload.manifest 不是由发布者签名的(或已被篡改)";
        return false;
    }
    return true;
}

// =====================================================================
// 极简 JSON 解析(只覆盖 launch.json / payload.manifest 需要的语法)
// =====================================================================
struct Json {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolean = false;
    double number = 0.0;
    std::string string;
    std::vector<Json> array;
    std::vector<std::pair<std::string, Json>> object;

    const Json* Find(const std::string& key) const {
        if (type != Type::Object) return nullptr;
        for (const auto& kv : object) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }
};

class JsonParser {
public:
    explicit JsonParser(const std::string& text) : text_(text) {}

    bool Parse(Json& out, std::wstring& err) {
        SkipSpace();
        if (!ParseValue(out, err)) return false;
        SkipSpace();
        if (pos_ != text_.size()) {
            err = L"JSON 末尾存在多余内容";
            return false;
        }
        return true;
    }

private:
    void SkipSpace() {
        while (pos_ < text_.size()) {
            unsigned char c = static_cast<unsigned char>(text_[pos_]);
            if (c == ' ' || c == '\t' || c == '\r' || c == '\n') {
                ++pos_;
            } else {
                break;
            }
        }
    }

    bool Fail(std::wstring& err, const wchar_t* message) {
        err = message;
        return false;
    }

    bool ParseValue(Json& out, std::wstring& err) {
        SkipSpace();
        if (pos_ >= text_.size()) return Fail(err, L"JSON 提前结束");
        char c = text_[pos_];
        if (c == '{') return ParseObject(out, err);
        if (c == '[') return ParseArray(out, err);
        if (c == '"') {
            out.type = Json::Type::String;
            return ParseString(out.string, err);
        }
        if (c == 't') {
            if (text_.compare(pos_, 4, "true") == 0) {
                pos_ += 4;
                out.type = Json::Type::Bool;
                out.boolean = true;
                return true;
            }
            return Fail(err, L"JSON 布尔值错误");
        }
        if (c == 'f') {
            if (text_.compare(pos_, 5, "false") == 0) {
                pos_ += 5;
                out.type = Json::Type::Bool;
                out.boolean = false;
                return true;
            }
            return Fail(err, L"JSON 布尔值错误");
        }
        if (c == 'n') {
            if (text_.compare(pos_, 4, "null") == 0) {
                pos_ += 4;
                out.type = Json::Type::Null;
                return true;
            }
            return Fail(err, L"JSON null 错误");
        }
        if (c == '-' || (c >= '0' && c <= '9')) return ParseNumber(out, err);
        return Fail(err, L"JSON 出现无法识别的值");
    }

    bool ParseObject(Json& out, std::wstring& err) {
        out.type = Json::Type::Object;
        ++pos_;  // {
        SkipSpace();
        if (pos_ < text_.size() && text_[pos_] == '}') {
            ++pos_;
            return true;
        }
        for (;;) {
            SkipSpace();
            if (pos_ >= text_.size() || text_[pos_] != '"') return Fail(err, L"JSON 对象缺少键");
            std::string key;
            if (!ParseString(key, err)) return false;
            SkipSpace();
            if (pos_ >= text_.size() || text_[pos_] != ':') return Fail(err, L"JSON 对象缺少冒号");
            ++pos_;
            Json value;
            if (!ParseValue(value, err)) return false;
            out.object.emplace_back(std::move(key), std::move(value));
            SkipSpace();
            if (pos_ < text_.size() && text_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < text_.size() && text_[pos_] == '}') {
                ++pos_;
                return true;
            }
            return Fail(err, L"JSON 对象缺少逗号或右花括号");
        }
    }

    bool ParseArray(Json& out, std::wstring& err) {
        out.type = Json::Type::Array;
        ++pos_;  // [
        SkipSpace();
        if (pos_ < text_.size() && text_[pos_] == ']') {
            ++pos_;
            return true;
        }
        for (;;) {
            Json value;
            if (!ParseValue(value, err)) return false;
            out.array.push_back(std::move(value));
            SkipSpace();
            if (pos_ < text_.size() && text_[pos_] == ',') {
                ++pos_;
                continue;
            }
            if (pos_ < text_.size() && text_[pos_] == ']') {
                ++pos_;
                return true;
            }
            return Fail(err, L"JSON 数组缺少逗号或右方括号");
        }
    }

    bool ParseHex4(unsigned& value) {
        if (pos_ + 4 > text_.size()) return false;
        value = 0;
        for (int i = 0; i < 4; ++i) {
            char c = text_[pos_ + i];
            value <<= 4;
            if (c >= '0' && c <= '9') value |= static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') value |= static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') value |= static_cast<unsigned>(c - 'A' + 10);
            else return false;
        }
        pos_ += 4;
        return true;
    }

    static void AppendUtf8(std::string& out, unsigned cp) {
        if (cp <= 0x7F) {
            out.push_back(static_cast<char>(cp));
        } else if (cp <= 0x7FF) {
            out.push_back(static_cast<char>(0xC0 | (cp >> 6)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else if (cp <= 0xFFFF) {
            out.push_back(static_cast<char>(0xE0 | (cp >> 12)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        } else {
            out.push_back(static_cast<char>(0xF0 | (cp >> 18)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 12) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | ((cp >> 6) & 0x3F)));
            out.push_back(static_cast<char>(0x80 | (cp & 0x3F)));
        }
    }

    bool ParseString(std::string& out, std::wstring& err) {
        ++pos_;  // "
        out.clear();
        for (;;) {
            if (pos_ >= text_.size()) return Fail(err, L"JSON 字符串没有结束");
            char c = text_[pos_++];
            if (c == '"') return true;
            if (static_cast<unsigned char>(c) < 0x20) return Fail(err, L"JSON 字符串包含控制字符");
            if (c != '\\') {
                out.push_back(c);
                continue;
            }
            if (pos_ >= text_.size()) return Fail(err, L"JSON 转义没有结束");
            char e = text_[pos_++];
            switch (e) {
            case '"': out.push_back('"'); break;
            case '\\': out.push_back('\\'); break;
            case '/': out.push_back('/'); break;
            case 'b': out.push_back('\b'); break;
            case 'f': out.push_back('\f'); break;
            case 'n': out.push_back('\n'); break;
            case 'r': out.push_back('\r'); break;
            case 't': out.push_back('\t'); break;
            case 'u': {
                unsigned cp = 0;
                if (!ParseHex4(cp)) return Fail(err, L"JSON \\u 转义错误");
                if (cp >= 0xD800 && cp <= 0xDBFF && pos_ + 6 <= text_.size()
                    && text_[pos_] == '\\' && text_[pos_ + 1] == 'u') {
                    size_t save = pos_;
                    pos_ += 2;
                    unsigned low = 0;
                    if (ParseHex4(low) && low >= 0xDC00 && low <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                    } else {
                        pos_ = save;
                    }
                }
                AppendUtf8(out, cp);
                break;
            }
            default:
                return Fail(err, L"JSON 字符串包含未知转义");
            }
        }
    }

    bool ParseNumber(Json& out, std::wstring& err) {
        size_t start = pos_;
        if (text_[pos_] == '-') ++pos_;
        while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
        if (pos_ < text_.size() && text_[pos_] == '.') {
            ++pos_;
            while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
        }
        if (pos_ < text_.size() && (text_[pos_] == 'e' || text_[pos_] == 'E')) {
            ++pos_;
            if (pos_ < text_.size() && (text_[pos_] == '+' || text_[pos_] == '-')) ++pos_;
            while (pos_ < text_.size() && text_[pos_] >= '0' && text_[pos_] <= '9') ++pos_;
        }
        std::string number = text_.substr(start, pos_ - start);
        char* end = nullptr;
        out.number = std::strtod(number.c_str(), &end);
        if (end == number.c_str()) return Fail(err, L"JSON 数字格式错误");
        out.type = Json::Type::Number;
        return true;
    }

    const std::string& text_;
    size_t pos_ = 0;
};

// =====================================================================
// 路径与清单
// =====================================================================
static bool IsWithinRoot(const fs::path& root, const fs::path& candidate) {
    std::error_code ec;
    fs::path r = fs::weakly_canonical(root, ec);
    if (ec) return false;
    fs::path c = fs::weakly_canonical(candidate, ec);
    if (ec) return false;
    std::wstring rs = r.wstring();
    std::wstring cs = c.wstring();
    if (cs.size() < rs.size()) return false;
    if (_wcsnicmp(cs.c_str(), rs.c_str(), rs.size()) != 0) return false;
    if (cs.size() == rs.size()) return true;
    wchar_t next = cs[rs.size()];
    return next == L'\\' || next == L'/';
}

struct ManifestEntry {
    std::wstring path;    // 相对程序根目录, 使用 '/'
    std::wstring sha256;  // 小写十六进制
};

struct Manifest {
    std::vector<ManifestEntry> entries;
};

static bool LoadManifest(const fs::path& binDir, Manifest& out, std::wstring& err) {
    std::vector<unsigned char> manifestBytes;
    if (!ReadFileBytes(binDir / L"payload.manifest", manifestBytes)) {
        err = L"缺少 Bin\\payload.manifest, 请先运行 tools\\sign.ps1";
        return false;
    }
    std::string sigText;
    if (!ReadFileText(binDir / L"payload.sig", sigText)) {
        err = L"缺少 Bin\\payload.sig, 请先运行 tools\\sign.ps1";
        return false;
    }
    std::vector<unsigned char> signature;
    if (!Base64Decode(TrimAscii(sigText), signature)) {
        err = L"Bin\\payload.sig 不是有效的 Base64";
        return false;
    }
    if (!VerifyRsaSignature(manifestBytes, signature, err)) return false;

    std::string text(manifestBytes.begin(), manifestBytes.end());
    StripUtf8Bom(text);
    Json root;
    if (!JsonParser(text).Parse(root, err)) return false;
    const Json* files = root.Find("files");
    if (!files || files->type != Json::Type::Array) {
        err = L"payload.manifest 缺少 files 数组";
        return false;
    }
    for (const Json& item : files->array) {
        const Json* p = item.Find("path");
        const Json* h = item.Find("sha256");
        if (!p || p->type != Json::Type::String || !h || h->type != Json::Type::String) {
            err = L"payload.manifest 的文件项格式错误";
            return false;
        }
        ManifestEntry entry;
        entry.path = Utf8ToWide(p->string);
        entry.sha256 = Utf8ToWide(h->string);
        for (wchar_t& ch : entry.sha256) ch = static_cast<wchar_t>(std::towlower(ch));
        out.entries.push_back(std::move(entry));
    }
    return true;
}

static bool VerifyPayload(const fs::path& root, const Manifest& manifest, std::wstring& err) {
    for (const ManifestEntry& entry : manifest.entries) {
        fs::path rel(entry.path);
        if (rel.is_absolute()) {
            err = L"清单中出现绝对路径: " + entry.path;
            return false;
        }
        fs::path full = root / rel;
        if (!IsWithinRoot(root, full)) {
            err = L"清单中的路径越界: " + entry.path;
            return false;
        }
        std::error_code ec;
        fs::path canonical = fs::weakly_canonical(full, ec);
        if (ec || !fs::is_regular_file(canonical, ec)) {
            err = L"清单中的文件不存在: " + entry.path;
            return false;
        }
        std::vector<unsigned char> data;
        if (!ReadFileBytes(canonical, data)) {
            err = L"读取文件失败: " + entry.path;
            return false;
        }
        unsigned char hash[32] = {};
        if (!Sha256Raw(data, hash, err)) return false;
        if (HexLower(hash, 32) != entry.sha256) {
            err = L"文件校验失败(可能被替换): " + entry.path;
            return false;
        }
    }
    return true;
}

static bool CheckInManifest(const fs::path& root, const Manifest& manifest,
                            const fs::path& file, const wchar_t* label, std::wstring& err) {
    std::error_code ec;
    fs::path r = fs::weakly_canonical(root, ec);
    if (ec) {
        err = L"程序目录无效";
        return false;
    }
    fs::path f = fs::weakly_canonical(file, ec);
    if (ec) {
        err = std::wstring(label) + L" 路径无效";
        return false;
    }
    fs::path relative = f.lexically_relative(r);
    if (relative.empty() || relative.native().rfind(L"..", 0) == 0) {
        err = std::wstring(label) + L" 不在程序目录内";
        return false;
    }
    std::wstring rel = relative.generic_wstring();
    for (const ManifestEntry& entry : manifest.entries) {
        if (_wcsicmp(entry.path.c_str(), rel.c_str()) == 0) return true;
    }
    err = std::wstring(label) + L" 不在签名清单中";
    return false;
}

// =====================================================================
// launch.json
// =====================================================================
struct CaseInsensitiveLess {
    bool operator()(const std::wstring& a, const std::wstring& b) const {
        return _wcsicmp(a.c_str(), b.c_str()) < 0;
    }
};

struct LaunchConfig {
    std::wstring runtime;
    std::wstring script;
    std::wstring args;  // 保留字段: 完整命令行? 下面用 vector
    std::vector<std::wstring> argumentList;
    std::wstring cwd;
    std::wstring log;
    bool inheritArgs = true;
    std::map<std::wstring, std::wstring, CaseInsensitiveLess> env;
};

static bool LoadLaunchConfig(const fs::path& jsonPath, LaunchConfig& out, std::wstring& err) {
    std::string text;
    if (!ReadFileText(jsonPath, text)) {
        err = L"找不到 Bin\\launch.json";
        return false;
    }
    StripUtf8Bom(text);
    Json root;
    if (!JsonParser(text).Parse(root, err)) {
        err = L"Bin\\launch.json 解析失败: " + err;
        return false;
    }

    auto readString = [&root](const char* key) -> std::wstring {
        const Json* value = root.Find(key);
        if (!value || value->type != Json::Type::String) return {};
        return Utf8ToWide(value->string);
    };

    out.runtime = readString("runtime");
    out.script = readString("script");
    out.cwd = readString("cwd");
    out.log = readString("log");
    if (out.runtime.empty() || out.script.empty()) {
        err = L"launch.json 缺少 runtime 或 script";
        return false;
    }

    if (const Json* args = root.Find("args")) {
        if (args->type != Json::Type::Array) {
            err = L"launch.json 的 args 必须是数组";
            return false;
        }
        for (const Json& item : args->array) {
            if (item.type != Json::Type::String) {
                err = L"launch.json 的 args 只能包含字符串";
                return false;
            }
            out.argumentList.push_back(Utf8ToWide(item.string));
        }
    }

    if (const Json* env = root.Find("env")) {
        if (env->type != Json::Type::Object) {
            err = L"launch.json 的 env 必须是对象";
            return false;
        }
        for (const auto& kv : env->object) {
            if (kv.second.type != Json::Type::String) {
                err = L"launch.json 的 env 值必须是字符串";
                return false;
            }
            out.env[Utf8ToWide(kv.first)] = Utf8ToWide(kv.second.string);
        }
    }

    if (const Json* inherit = root.Find("inherit_args")) {
        if (inherit->type != Json::Type::Bool) {
            err = L"launch.json 的 inherit_args 必须是布尔值";
            return false;
        }
        out.inheritArgs = inherit->boolean;
    }
    return true;
}

// =====================================================================
// 环境变量块
// =====================================================================
static bool BuildEnvironmentBlock(
    const std::map<std::wstring, std::wstring, CaseInsensitiveLess>& extra,
    std::vector<wchar_t>& out) {
    std::map<std::wstring, std::wstring, CaseInsensitiveLess> env;
    LPWCH block = GetEnvironmentStringsW();
    if (!block) return false;
    for (LPWCH p = block; *p; p += wcslen(p) + 1) {
        std::wstring entry(p);
        size_t eq = entry.find(L'=');
        if (eq == std::wstring::npos || eq == 0) continue;
        env[entry.substr(0, eq)] = entry.substr(eq + 1);
    }
    FreeEnvironmentStringsW(block);
    for (const auto& kv : extra) env[kv.first] = kv.second;

    out.clear();
    for (const auto& kv : env) {
        std::wstring line = kv.first + L'=' + kv.second;
        out.insert(out.end(), line.begin(), line.end());
        out.push_back(L'\0');
    }
    out.push_back(L'\0');
    return true;
}

// =====================================================================
// 命令行拼装与启动
// =====================================================================
static std::wstring QuoteArg(const std::wstring& arg) {
    if (arg.empty()) return L"\"\"";
    bool needQuote = arg.find_first_of(L" \t\n\v\"") != std::wstring::npos;
    if (!needQuote) return arg;

    std::wstring out;
    out.push_back(L'"');
    size_t backslashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++backslashes;
            continue;
        }
        if (c == L'"') {
            out.append(backslashes * 2 + 1, L'\\');
            out.push_back(L'"');
            backslashes = 0;
            continue;
        }
        out.append(backslashes, L'\\');
        backslashes = 0;
        out.push_back(c);
    }
    out.append(backslashes * 2, L'\\');
    out.push_back(L'"');
    return out;
}

// =====================================================================
// 入口
// =====================================================================
int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    const fs::path root(GetExeDir());
    if (root.empty()) {
        MessageBoxW(nullptr, L"无法获取程序目录", L"Nobplaypot", MB_OK | MB_ICONERROR);
        return 1;
    }
    const fs::path bin = root / L"Bin";
    LauncherLog log(bin / L"launch.log");
    log.Write(L"启动器开始, root=" + root.wstring());

    std::wstring err;
    Manifest manifest;
    if (!LoadManifest(bin, manifest, err)) {
        return FailWith(log, err);
    }
    log.Write(L"清单签名校验通过, 条目数=" + std::to_wstring(manifest.entries.size()));

    if (!VerifyPayload(root, manifest, err)) {
        return FailWith(log, err);
    }
    log.Write(L"payload 文件哈希全部通过");

    LaunchConfig config;
    if (!LoadLaunchConfig(bin / L"launch.json", config, err)) {
        return FailWith(log, err);
    }

    const auto resolve = [&root](const std::wstring& value) -> fs::path {
        fs::path p(value);
        return p.is_absolute() ? p : (root / p);
    };
    const fs::path runtime = resolve(config.runtime);
    const fs::path script = resolve(config.script);
    const fs::path cwd = config.cwd.empty() ? root : resolve(config.cwd);

    if (!IsWithinRoot(root, runtime) || !fs::is_regular_file(runtime)) {
        return FailWith(log, L"runtime 不存在或在程序目录外: " + runtime.wstring());
    }
    if (!IsWithinRoot(root, script) || !fs::is_regular_file(script)) {
        return FailWith(log, L"script 不存在或在程序目录外: " + script.wstring());
    }
    if (!IsWithinRoot(root, cwd) || !fs::is_directory(cwd)) {
        return FailWith(log, L"cwd 不存在或在程序目录外: " + cwd.wstring());
    }
    if (!CheckInManifest(root, manifest, runtime, L"runtime", err)) {
        return FailWith(log, err);
    }
    if (!CheckInManifest(root, manifest, script, L"script", err)) {
        return FailWith(log, err);
    }

    // Windows 的 CreateProcess 即使指定了 lpApplicationName, 子进程的 argv[0]
    // 仍取自 lpCommandLine。Python 把 argv[0] 当程序名、argv[1] 当脚本, 所以
    // 命令行必须以 runtime 自身开头, 否则 pythonw 会以为没有脚本而直接退出。
    std::wstring commandLine = QuoteArg(runtime.wstring()) + L' ' + QuoteArg(script.wstring());
    for (const std::wstring& arg : config.argumentList) {
        commandLine += L' ';
        commandLine += QuoteArg(arg);
    }
    if (config.inheritArgs) {
        int argc = 0;
        LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
        if (argv) {
            for (int i = 1; i < argc; ++i) {
                commandLine += L' ';
                commandLine += QuoteArg(argv[i]);
            }
            LocalFree(argv);
        }
    }

    std::vector<wchar_t> environment;
    bool hasEnvironment = false;
    if (!config.env.empty()) {
        if (!BuildEnvironmentBlock(config.env, environment)) {
            return FailWith(log, L"构造环境变量失败");
        }
        hasEnvironment = true;
    }

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    DWORD flags = CREATE_NO_WINDOW;
    if (hasEnvironment) flags |= CREATE_UNICODE_ENVIRONMENT;

    if (!CreateProcessW(runtime.c_str(), commandLine.data(), nullptr, nullptr, FALSE,
                        flags, hasEnvironment ? environment.data() : nullptr,
                        cwd.c_str(), &startup, &process)) {
        const DWORD code = GetLastError();
        wchar_t buffer[256] = {};
        std::swprintf(buffer, 256, L"启动 python 失败 (错误码 %lu)\n\n%s",
                      static_cast<unsigned long>(code), runtime.c_str());
        CloseHandle(process.hThread);
        return FailWith(log, buffer);
    }

    log.Write(L"已启动: " + runtime.wstring() + L" " + commandLine);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
