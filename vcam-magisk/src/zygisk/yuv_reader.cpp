#include "yuv_reader.h"

#include <android/log.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstring>

#define LOG_TAG "vcam-yuv"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  LOG_TAG, __VA_ARGS__)

namespace vcam {

namespace {

constexpr uint32_t kMagic = 0x564D4143;  // 'CAMV' little-endian
constexpr size_t kHeaderSize = 16;

// Candidate paths in priority order. The first that's readable wins.
constexpr const char* kCandidates[] = {
    "/data/local/tmp/vcam.yuv",
    "/data/data/com.livemobillrerun.vcam/files/vcam.yuv",
};

uint32_t ReadU32LE(const uint8_t* p) {
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8) |
           (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}

}  // namespace

YuvReader& YuvReader::instance() {
    static YuvReader r;
    return r;
}

bool YuvReader::Open() {
    std::lock_guard<std::mutex> lk(mu_);
    for (const char* candidate : kCandidates) {
        struct stat st {};
        if (::stat(candidate, &st) == 0 && (st.st_mode & S_IFREG)) {
            path_ = candidate;
            LOGI("opened %s (%lld bytes)", candidate,
                 static_cast<long long>(st.st_size));
            return true;
        }
    }
    LOGW("no vcam.yuv found at any candidate path");
    return false;
}

bool YuvReader::ReadLatest(
    const uint8_t** out_y, const uint8_t** out_u, const uint8_t** out_v,
    int* out_width, int* out_height, uint32_t* out_frame_index) {
    std::lock_guard<std::mutex> lk(mu_);
    if (path_.empty()) return false;

    // Reopen on every read because the writer atomically renames the
    // tmpfile over the target — our previous fd would be pinned to a
    // dead inode otherwise.
    int fd = ::open(path_.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;

    struct stat st {};
    if (::fstat(fd, &st) != 0 || st.st_size < (off_t)kHeaderSize) {
        ::close(fd);
        return false;
    }
    const size_t total = static_cast<size_t>(st.st_size);
    if (buf_.size() < total) buf_.resize(total);
    ssize_t n = ::read(fd, buf_.data(), total);
    ::close(fd);
    if (n != static_cast<ssize_t>(total)) return false;

    const uint8_t* p = buf_.data();
    if (ReadU32LE(p) != kMagic) {
        LOGW("bad magic 0x%08x", ReadU32LE(p));
        return false;
    }
    // Header layout — must match `YuvFileWriter.kt`:
    //   [0..3]   magic
    //   [4..7]   width
    //   [8..11]  height
    //   [12..15] frame_counter
    int w = static_cast<int>(ReadU32LE(p + 4));
    int h = static_cast<int>(ReadU32LE(p + 8));
    const uint32_t frame_idx = ReadU32LE(p + 12);

    const size_t expected_payload = static_cast<size_t>(w) * h * 3 / 2;
    if (expected_payload + kHeaderSize > total) {
        // Header values look bogus; pretend it's a 1280x720 frame.
        w = 1280;
        h = 720;
    }

    const uint8_t* base = p + kHeaderSize;
    const size_t y_plane = static_cast<size_t>(w) * h;
    const size_t uv_plane = y_plane / 4;

    if (out_y) *out_y = base;
    if (out_u) *out_u = base + y_plane;
    if (out_v) *out_v = base + y_plane + uv_plane;
    if (out_width) *out_width = w;
    if (out_height) *out_height = h;
    if (out_frame_index) *out_frame_index = frame_idx;
    width_ = w;
    height_ = h;
    last_frame_index_ = frame_idx;
    return true;
}

}  // namespace vcam
