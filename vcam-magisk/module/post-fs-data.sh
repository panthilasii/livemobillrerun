#!/system/bin/sh
# Runs at early boot, before zygote starts.
# Make /data/local/tmp/vcam.yuv readable by cameraserver and apps.

YUV=/data/local/tmp/vcam.yuv

# Create empty file if missing so the HAL hook always has something to mmap.
if [ ! -f "$YUV" ]; then
  touch "$YUV"
fi

chmod 644 "$YUV" 2>/dev/null
chcon u:object_r:system_data_file:s0 "$YUV" 2>/dev/null || true
