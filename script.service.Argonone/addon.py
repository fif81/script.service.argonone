import os
import sys
import threading
import time
import xbmc
import xbmcaddon
import xbmcvfs
import RPi.GPIO as GPIO

# look for system-tools and add to library path - required for smbus
for specialDir in ['special://home', 'special://xbmc']:
    libpath = os.path.join(xbmcvfs.translatePath(specialDir), 'addons/virtual.system-tools/lib')
    if os.path.isdir(libpath):
        sys.path.append(libpath)
        break

import smbus

class ArgonControl(xbmc.Monitor):

    def __init__(self):
        # addon name used for logging
        self.name = xbmcaddon.Addon().getAddonInfo('name')

        # next time cpu temperature is checked for fan control      
        self.nextUpdate = time.time()

        # was there a notification 'System.OnQuit'?
        self.systemQuitting = False

        # was there a notification 'System.OnRestart'?
        self.systemRestarting = False

        # is this addon stopping or stopped?
        self.stopping = False

        # did the user change the settings since last cpu temperature check?
        self.settingsChanged = False

        # time when a rising edge on power button signal was detected
        self.pwrRiseTime = None

        # load settings initially
        self.loadSettings()

        # used for synchronisation of stopAddon(), smbus writes and wait() in temperature check loop
        self.cv = threading.Condition(lock = threading.Lock())
        
         # initialize smbus for fan control
        self.smbus = smbus.SMBus(0 if GPIO.RPI_INFO['P1_REVISION'] == 1 else 1)     

        # initialize GPIO library and shutdown/reset button pin. Activate pull-down-resistor for shutdown/reset pin
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM) 
        GPIO.setup(4, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)

        try:
            # watch GPIO pin used for shutdown/reset button
            def onSignalEdge(gpio):
                self.onSignalEdge(gpio)
            GPIO.remove_event_detect(4)
            GPIO.add_event_detect(4, GPIO.BOTH, callback = onSignalEdge)
        except:
            # if something goes wrong, remove pull-down-resistor to avoid short-circuits
            GPIO.cleanup(4)
            raise

    def loadSettings(self):
        addon = xbmcaddon.Addon()
        self.checkInterval = int(addon.getSetting('checkinterval'))
        self.numberOfChecks = int(addon.getSetting('numberofchecks'))
        previousValue = -1
        powerMap = []
        for label in [
                (-274, 'fanpowermin'),
                (30, 'fanpower30'),
                (40, 'fanpower40'),
                (45, 'fanpower45'),
                (50, 'fanpower50'),
                (55, 'fanpower55'),
                (60, 'fanpower60'),
                (65, 'fanpower65'),
                (70, 'fanpower70'),
                (75, 'fanpower75'),
                (80, 'fanpower80')]:
            powerValue = int(addon.getSetting(label[1]))
            if previousValue < powerValue:
                powerMap.insert(0, (label[0], powerValue))
                xbmc.log("## {0} ## added thresold to mapping: {1} -> {2}".format(self.name, label[0], powerValue), level = xbmc.LOGDEBUG)
                previousValue = powerValue
            elif previousValue > powerValue:
                xbmc.log("## {0} ## ignoring thresold {1} -> {2} because previous fan power setting(s) was/were higher!".format(self.name, label[0], powerValue), level = xbmc.LOGDEBUG)     
        self.tempToFanPower = powerMap
        xbmc.log("## {0} ## reloaded settings".format(self.name), level = xbmc.LOGDEBUG)

    def onSettingsChanged(self):
        xbmc.log("## {0} ## entered onSettingsChanged".format(self.name), level = xbmc.LOGDEBUG)
        with self.cv:
            self.loadSettings()
            self.settingsChanged = True
            self.cv.notify_all()

    def onNotification(self, sender, method, data):
        xbmc.log("## {0} ## got event =>>>> {1} from: {2} with data: {3}".format(self.name, method, sender, data), level = xbmc.LOGDEBUG)

        # Kodi's termination event
        if method == 'System.OnQuit':
            self.systemQuitting = True
            self.stopAddon()

        # Kodi's restart event - System.OnQuit follows
        elif method == 'System.OnRestart':
            self.systemRestarting = True
            self.stopAddon()

    def onSignalEdge(self, gpio):
        xbmc.log("## {0} ## gpio edge detected on pin {1}!".format(self.name, gpio), level = xbmc.LOGDEBUG)
        currentTime = time.time_ns()
        level = GPIO.input(gpio)
        if level == GPIO.HIGH:
            self.pwrRiseTime = currentTime
        elif self.pwrRiseTime != None:
            signalLength = currentTime - self.pwrRiseTime
            xbmc.log("## {0} ## signal detected! Duration: {1} ns".format(self.name, signalLength), level = xbmc.LOGDEBUG)
            if signalLength < 30000000:
                xbmc.log("## {0} ## I will trigger a restart!".format(self.name, signalLength), level = xbmc.LOGDEBUG)
                xbmc.restart()
            else:
                xbmc.log("## {0} ## I will trigger a shutdown!".format(self.name, signalLength), level = xbmc.LOGDEBUG)
                xbmc.shutdown()

    def stopAddon(self):
        if not self.stopping:
            with self.cv:
                if not self.stopping:
                    try:
                        xbmc.log("## {0} ## method stopAddon called".format(self.name), level = xbmc.LOGDEBUG)
                        xbmc.log("## {0} ## disabling event detection".format(self.name), level = xbmc.LOGDEBUG)
                         
                        # stop listening to power button signal
                        GPIO.remove_event_detect(4)
                        
                        # cleanup GPIO configuration is recommended
                        GPIO.cleanup(4)

                        # fan off (0%)
                        xbmc.log("## {0} ## switching fan off".format(self.name), level = xbmc.LOGDEBUG)
                        self.smbus.write_byte(0x1a, 0x00)

                        # ready for power cut? - watches UART
                        if self.systemQuitting and not self.systemRestarting:
                            xbmc.log("## {0} ## sending signal to make case watching UART for power cut".format(self.name), level = xbmc.LOGDEBUG)
                            self.smbus.write_byte(0x1a, 0xFF)

                        # close smbus connection
                        self.smbus.close()

                    finally:
                        self.stopping = True
                        self.cv.notify_all()

    def monitorCpuTemperature(self): 

        tempCache = []
        currentFanValue = -1

        while True:
            with self.cv:

                # check for stop
                if self.stopping:
                    xbmc.log("## {0} ## stop check was true".format(self.name), level = xbmc.LOGDEBUG)
                    break

                # wait
                now = time.time()
                while not self.stopping and not self.settingsChanged and self.nextUpdate > now:
                    timeOut = self.nextUpdate - now
                    xbmc.log("## {0} ## wait for {1} seconds ...".format(self.name, timeOut), level = xbmc.LOGDEBUG)
                    self.cv.wait(timeout=timeOut)
                    now = time.time()

                # check for stop
                if self.stopping:
                    xbmc.log("## {0} ## stop check was true".format(self.name), level = xbmc.LOGDEBUG)
                    break

            # get cpu temperature
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as fd:
                cpuTemp = int(fd.read()) // 1000
                xbmc.log("## {0} ## measured temperature - value is {1} degree celcius".format(self.name, cpuTemp), level = xbmc.LOGDEBUG)

            # save temperature value, drop oldest value(s) if necessary
            tempCache.append(cpuTemp)
            while len(tempCache) > self.numberOfChecks:
                tempCache.pop(0)

            # highest temperature value is relevant
            for tempValue in tempCache:
                if tempValue > cpuTemp:
                    cpuTemp = tempValue
            xbmc.log("## {0} ## determined {1} degree celcius as relevant value for further processing".format(self.name, cpuTemp), level = xbmc.LOGDEBUG)
     
            # find matching setting for fan power and send to case
            tempToFanPower = self.tempToFanPower
            fanValue = 0   
            for setting in tempToFanPower:
                if cpuTemp >= setting[0]:
                    fanValue = setting[1]
                    # send value only if changed
                    if currentFanValue != fanValue:
                        xbmc.log("## {0} ## will set fan power to {1}%".format(self.name, fanValue), level = xbmc.LOGDEBUG)
                        with self.cv:
                            if self.stopping:
                                xbmc.log("## {0} ## setting fan power skipped due to module stop!".format(self.name), level = xbmc.LOGDEBUG)
                            else:
                                self.smbus.write_byte(0x1a, fanValue)
                                currentFanValue = fanValue
                    else:
                        xbmc.log("## {0} ## fan power is already {1}%".format(self.name, fanValue), level = xbmc.LOGDEBUG)
                    break

            # when do we have to check the fan power next?
            self.nextUpdate = now + self.checkInterval
            
            # reset setting flag
            self.settingsChanged = False


if __name__ == '__main__':
    argonControl = ArgonControl()
    try:
        threading.Thread(target = argonControl.monitorCpuTemperature).start()
        argonControl.waitForAbort()
    finally:
        argonControl.stopAddon()  

