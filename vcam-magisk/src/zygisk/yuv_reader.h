#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace vcam {

// Reads YUV420 (I420) frames from the file written by vcam-app.
//
// File format (matches `YuvFileWriter.kt` on the Android side):
//   bytes  0..3   : magic 'VCAM' (0x564D4143 little-endian)
//   bytes  4..7   : width  (uint32_le)
//   bytes  8..11  : height (uint32_le)
//   bytes 12..15  : frame counter (uint32_le, monotonically increasing)
//   bytes 16..    : Y plane (w*h)         | tightly packed I420
//                   U plane (w*h/4)       |
//                   V plane (w*h/4)       |
//
// The file is rewritten in-place on every frame via atomic rename, so
// readers see either the previous frame or the new frame — never a torn
// half-frame. Always re-open the file (or stat for mtime change) before
// reading; mmap is unsafe because the inode changes on each rotation.

class YuvReader {
public:
    static YuvReader& instance();

    // Discover which file path is present on the device. Tries the
    // canonical `/data/local/tmp/vcam.yuv` first, then the app-private
    // fallback. Returns false if neither is readable.
    bool Open();

    // Re-read the latest frame. Returns false on missing/corrupt file.
    // On success `out_*` are valid until the next call.
    bool ReadLatest(
        const uint8_t** out_y,
        const uint8_t** out_u,
        const uint8_t** out_v,
        int* out_width,
        int* out_height,
        uint32_t* out_frame_index);

    const std::string& path() const { return path_; }

private:
    YuvReader() = default;

    std::mutex mu_;
    std::string path_;
    std::vector<uint8_t> buf_;
    int width_ = 0;
    int height_ = 0;
    uint32_t last_frame_index_ = 0;
};

}  // namespace vcam
