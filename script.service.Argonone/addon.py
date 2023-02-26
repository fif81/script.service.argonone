import os
import sys
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

class FanControl(xbmc.Monitor):

    def __init__(self):
        self.addon = xbmcaddon.Addon()
        self.name = self.addon.getAddonInfo('name')
        self.loadSettings()
        self.main()

    def loadSettings(self):
        self.checkInterval = int(self.addon.getSetting('checkinterval'))
        self.numberOfChecks = int(self.addon.getSetting('numberofchecks'))
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
            powerValue = int(self.addon.getSetting(label[1]))
            if previousValue < powerValue:
                powerMap.insert(0, (label[0], powerValue))
                xbmc.log("## {0} ## added thresold to mapping: {1} -> {2}".format(self.name, label[0], powerValue), level = xbmc.LOGDEBUG)
                previousValue = powerValue
            elif previousValue > powerValue:
                xbmc.log("## {0} ## ignoring thresold {1} -> {2} because previous fan power setting(s) was/were higher!".format(self.name, label[0], powerValue), level = xbmc.LOGDEBUG)     
        self.tempToFanPower = powerMap
        self.skipWaitForAbort = True
        xbmc.log("## {0} ## reloaded settings".format(self.name), level = xbmc.LOGDEBUG)

    def onSettingsChanged(self):
        self.loadSettings()
        self.skipWaitForAbort = True

    def onNotification(self, sender, method, data):
        xbmc.log("## {0} ## got event =>>>> {1} from: {2} with data: {3}".format(self.name, method, sender, data), level = xbmc.LOGDEBUG)
        if method == 'System.OnQuit':
            self.systemOnShutdown = True
        elif method == 'System.OnRestart':
            self.systemOnRestart = True

    def onSignalEdge(self, gpio):
        xbmc.log("## {0} ## gpio edge detected on pin {1}!".format(self.name, gpio), level = xbmc.LOGDEBUG)
        currentTime = time.time()
        level = GPIO.input(gpio)
        if level == GPIO.HIGH:
            self.PwrRiseTime = currentTime
        elif self.PwrRiseTime != None:
            signalLength = currentTime - self.PwrRiseTime
            xbmc.log("## {0} ## signal detected! Duration: {1}".format(self.name, signalLength), level = xbmc.LOGDEBUG)
            if signalLength < 0.03:
                xbmc.log("## {0} ## I will trigger a restart!".format(self.name, signalLength), level = xbmc.LOGDEBUG)
                xbmc.restart()
            else:
                xbmc.log("## {0} ## I will trigger a shutdown!".format(self.name, signalLength), level = xbmc.LOGDEBUG)
                xbmc.shutdown()

    def main(self):
        # This is for fan control
        self.bus = smbus.SMBus(0 if GPIO.RPI_INFO['P1_REVISION'] == 1 else 1)  

        # This is for power button control
        self.PwrRiseTime = None
        PWR_BUTTON = 4
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM) 
        GPIO.setup(PWR_BUTTON, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
        def onSignalEdge(gpio):
            self.onSignalEdge(gpio)
        GPIO.remove_event_detect(PWR_BUTTON)
        GPIO.add_event_detect(PWR_BUTTON, GPIO.BOTH, callback = onSignalEdge)

        tempCache = []
        currentFanValue = -1
        self.skipWaitForAbort = True
        self.systemOnShutdown = False
        self.systemOnRestart = False
        while not self.abortRequested():

            # if settings change we have to recalculate fan power so we do not waitForAbort(self.checkInterval)
            # self.skipWaitForAbort is True after settings have changed
            waitTime = self.checkInterval
            abortRequestedDuringWait = False
            while not abortRequestedDuringWait and waitTime > 0 and not self.skipWaitForAbort:
                shortWait = 5 if waitTime > 5 else waitTime
                waitTime -= shortWait
                xbmc.log("## {0} ## waiting for {1} seconds - further {2} seconds to wait left".format(self.name, shortWait, waitTime), level = xbmc.LOGDEBUG)
                abortRequestedDuringWait = self.waitForAbort(shortWait)

            # if abort was requested while waiting, lease main loop, too!
            if abortRequestedDuringWait:
                break

            # reset to False for next loop run
            self.skipWaitForAbort = False

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
                        self.bus.write_byte(0x1a, fanValue)
                        currentFanValue = fanValue
                    else:
                        xbmc.log("## {0} ## fan power is already {1}%".format(self.name, fanValue), level = xbmc.LOGDEBUG)
                    break
                
        # termination - say goodbye ..!    
        xbmc.log("## {0} ## got abort request - will stop fan".format(self.name), level = xbmc.LOGDEBUG)
        # stop listening to power button signal
        GPIO.remove_event_detect(PWR_BUTTON)
        # fan off (0%)
        self.bus.write_byte(0x1a, 0x00)
        # ready for power cut? - watches UART
        if self.systemOnShutdown and not self.systemOnRestart:
            xbmc.log("## {0} ## sending signal to make case watching UART for power cut".format(self.name), level = xbmc.LOGDEBUG)
            self.bus.write_byte(0x1a, 0xFF)

if __name__ == '__main__':
    FanControl()    

