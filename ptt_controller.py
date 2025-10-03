import logging
import os

class CM108PTT:
    def __init__(self, device="/dev/hidraw0", pin=3):
        self.device = device
        self.pin = pin
        self.working = True
        logging.debug(f"[CM108PTT] Initialized PTT on {self.device}, GPIO pin {self.pin}")

    def _set_gpio(self, state):
        if not 1 <= self.pin <= 8:
            logging.error(f"CM108PTT: GPIO pin must be 1–8, got {self.pin}")
            self.working = False
            return
        iomask = 1 << (self.pin - 1)
        iodata = state << (self.pin - 1)
        buf = bytes([0x00, 0x00, iomask, iodata, 0x00])
        try:
            with open(self.device, "wb", buffering=0) as f:
                f.write(buf)
            self.working = True
        except Exception as e:
            logging.error(f"CM108PTT: FAILED to write to {self.device}: {e}")
            self.working = False 

    def key(self):
        try:
            self._set_gpio(1)
        except Exception as e:
            logging.error(f"CM108PTT: key() error: {e}")


    def unkey(self):
        try:
            self._set_gpio(0)
        except Exception as e:
            logging.error(f"CM108PTT: unkey() error: {e}")

class PTTManager:
    def __init__(self, config):
    # PTT Setup (dual + single + legacy)
        self.config = config
        ptt_cfg = self.config.config.get("ptt", {})
        self.ptt = None
        self.ptt_2 = None
        self.ptt_mode = "VOX"
        self.ptt_2_mode = "VOX"

        if ptt_cfg.get("dual_ptt", False):
        # Dual PTT mode: read both
            primary_cfg = ptt_cfg.get("primary", {})
            secondary_cfg = ptt_cfg.get("secondary", {})

            if primary_cfg.get("mode", "").upper() == "CM108":
                self.ptt = CM108PTT(
                    device=primary_cfg.get("device_path", "/dev/hidraw0"),
                    pin=int(primary_cfg.get("gpio_pin", 3))
                )
                self.ptt_mode = "CM108"
                logging.info(f"Primary PTT (CM108) on {self.ptt.device}, GPIO {self.ptt.pin}")
            else:
                self.ptt = None
                self.ptt_mode = "VOX"
                logging.info("Primary PTT mode set to VOX (no GPIO control)")

            if secondary_cfg.get("mode", "").upper() == "CM108":
                self.ptt_2 = CM108PTT(
                    device=secondary_cfg.get("device_path", "/dev/hidraw3"),
                    pin=int(secondary_cfg.get("gpio_pin", 3))
                )
                self.ptt_2_mode = "CM108"
                logging.info(f"Secondary PTT (CM108) on {self.ptt_2.device}, GPIO {self.ptt_2.pin}")
            else:
                self.ptt_2 = None
                self.ptt_2_mode = "VOX"
                logging.info("Secondary PTT mode set to VOX (no GPIO control)")
            logging.info(f"[PTT] init secondary: mode={self.ptt_2_mode}, dev={getattr(self.ptt_2,'device',None)}, pin={getattr(self.ptt_2,'pin',None)}")


        else:
        # Single PTT mode: check for new-style `primary` first, fallback to legacy
            primary_cfg = ptt_cfg.get("primary", ptt_cfg)

            if primary_cfg.get("mode", "").upper() == "CM108":
                self.ptt = CM108PTT(
                    device=primary_cfg.get("device_path", "/dev/hidraw0"),
                    pin=int(primary_cfg.get("gpio_pin", 3))
                )
                self.ptt_mode = "CM108"
                logging.info(f"PTT mode set to CM108 (device={self.ptt.device}, pin={self.ptt.pin})")
            else:
                self.ptt = None
                self.ptt_mode = "VOX"
                logging.info("PTT mode set to VOX (no GPIO control)")

    def safe_ptt_key(self):
        primary_success = False
        secondary_success = False

        # PRIMARY PTT
        if self.ptt_mode == "CM108" and self.ptt:
            try:
                if getattr(self.ptt, "working", True):
                    self.ptt.key()
                    primary_success = True
            except Exception as e:
                logging.error(f"Primary PTT key error: {e}")
                self.ptt = None
                self.ptt_mode = "VOX"
                self.ptt_fallback = True
                logging.warning("Primary PTT failed — fallback to VOX")

        # SECONDARY PTT
        if getattr(self, "ptt_2_mode", "NONE") == "CM108" and self.ptt_2:
            try:
                if getattr(self.ptt_2, "working", True):
                    self.ptt_2.key()
                    logging.info("Secondary PTT keyed")
                    secondary_success = True
            except Exception as e:
                logging.error(f"Secondary PTT key error: {e}")
                self.ptt_2 = None
                self.ptt_2_mode = "VOX"
                self.ptt_2_fallback = True
                logging.warning("Secondary PTT failed — fallback to VOX")

        return primary_success or secondary_success

    def safe_ptt_unkey(self):
        primary_success = False
        secondary_success = False

        if self.ptt_mode == "CM108" and self.ptt:
            try:
                if getattr(self.ptt, "working", True):
                    self.ptt.unkey()
                    primary_success = True
            except Exception as e:
                logging.error(f"Primary PTT unkey error: {e}")
                self.ptt = None
                self.ptt_mode = "VOX"
                self.ptt_fallback = True
                logging.warning("Primary PTT unkey failed — fallback to VOX")

        if getattr(self, "ptt_2_mode", "NONE") == "CM108" and self.ptt_2:
            try:
                if getattr(self.ptt_2, "working", True):
                    self.ptt_2.unkey()
                    secondary_success = True
            except Exception as e:
                logging.error(f"Secondary PTT unkey error: {e}")
                self.ptt_2 = None
                self.ptt_2_mode = "VOX"
                self.ptt_2_fallback = True
                logging.warning("Secondary PTT unkey failed — fallback to VOX")

        return primary_success or secondary_success
    
    def get_ptt_status(self):
        ptt_cfg = self.config.config.get("ptt", {})
        dual_mode = ptt_cfg.get("dual_ptt", False)

        statuses = []

        # Determine where to look for primary PTT config
        primary_cfg = ptt_cfg.get("primary", ptt_cfg)
        device1 = primary_cfg.get("device_path", "/dev/hidraw0")
        mode1 = primary_cfg.get("mode", "NONE").upper()

        if self.ptt:
            if not os.path.exists(device1):
                statuses.append((f"Primary: Device Missing ({device1})", "red"))
            else:
                try:
                    with open(device1, "wb", buffering=0):
                        pass
                    if getattr(self.ptt, "working", True):
                        statuses.append((f"Primary: OK ({device1})", "green"))
                    else:
                        statuses.append((f"Primary: Detected, Last Op Failed ({device1})", "orange"))
                except PermissionError:
                    statuses.append((f"Primary: Permission Denied ({device1})", "orange"))
                except Exception:
                    statuses.append((f"Primary: Unusable ({device1})", "orange"))
        else:
            if mode1 == "CM108":
                statuses.append((f"Primary: Not Configured ({device1})", "red"))
            else:
                statuses.append(("Primary: VOX Mode", "blue"))

        # If not dual PTT, return now
        if not dual_mode:
            return statuses[0]

        # Check secondary
        secondary_cfg = ptt_cfg.get("secondary", {})
        device2 = secondary_cfg.get("device_path", "/dev/hidraw1")
        mode2 = secondary_cfg.get("mode", "NONE").upper()

        if self.ptt_2:
            if not os.path.exists(device2):
                statuses.append((f"Secondary: Device Missing ({device2})", "red"))
            else:
                try:
                    with open(device2, "wb", buffering=0):
                        pass
                    if getattr(self.ptt_2, "working", True):
                        statuses.append((f"Secondary: OK ({device2})", "green"))
                    else:
                        statuses.append((f"Secondary: Detected, Last Op Failed ({device2})", "orange"))
                except PermissionError:
                    statuses.append((f"Secondary: Permission Denied ({device2})", "orange"))
                except Exception:
                    statuses.append((f"Secondary: Unusable ({device2})", "orange"))
        else:
            if mode2 == "CM108":
                statuses.append((f"Secondary: Not Configured ({device2})", "red"))
            else:
                statuses.append(("Secondary: VOX Mode", "blue"))

        # Combine both into one string for GUI
        combined_status = " | ".join([s[0] for s in statuses])
        color = "green"
        if any(c == "red" for _, c in statuses):
            color = "red"
        elif any(c == "orange" for _, c in statuses):
            color = "orange"
        elif all(c == "blue" for _, c in statuses):
            color = "blue"
        return (combined_status, color)


