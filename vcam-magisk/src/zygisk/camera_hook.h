#pragma once

namespace vcam::camera {

// Install inline hooks on the Camera2 framework so apps that ask the
// system for a camera image instead get bytes from /data/local/tmp/vcam.yuv.
//
// Strategy A — JNI hook (preferred, SoC-agnostic):
//   Resolve `android::camera2::CameraDeviceClient::onResultReceived`
//   via dlsym in libcameraservice.so, replace it with our trampoline.
//   Inside the trampoline we copy YuvReader::ReadLatest() into the
//   capture's Image buffer before forwarding to the original callback.
//
// Strategy B — HAL overlay (MTK Helio G81-Ultra / Helio G85, fallback):
//   Replace `/vendor/lib64/hw/camera.mt6769.so` with our wrapper that
//   loads the original as `.real` and intercepts
//   `getCameraDeviceInterface_V3_X` for the front camera ID.
//
// The skeleton ships only Strategy A's structure — the actual hook
// engine (Dobby / Substrate / shadowhook) is intentionally left out so
// you can drop in whichever you prefer without ripping out our scaffold.
//
// Returns false if the hook engine couldn't resolve a target symbol on
// this device — the caller (`main.cpp`) treats that as "fall through to
// real camera" rather than an outright failure.

bool InstallHooks();

void UninstallHooks();

}  // namespace vcam::camera
