#!/system/bin/sh
# Runs once when the user installs the module via Magisk Manager.
# Prints info, then Magisk takes care of copying files into place.

ui_print "  ============================================="
ui_print "    livemobillrerun — Virtual Camera Module"
ui_print "  ============================================="
ui_print " "
ui_print "  This module is intended for personal use on"
ui_print "  YOUR OWN device. Use responsibly."
ui_print " "
ui_print "  After install:"
ui_print "    1) Reboot the phone."
ui_print "    2) Install vcam-app companion APK."
ui_print "    3) Run PC streamer + adb reverse tcp:8888."
ui_print " "

# We don't do special unzipping here yet — let Magisk's default flow
# copy module/ → /data/adb/modules/livemobillrerun_vcam/
set_perm_recursive "$MODPATH" 0 0 0755 0644
