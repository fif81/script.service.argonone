import os
import sys
import subprocess
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

class ArgonSettings():

    def __init__(self):
        addon = xbmcaddon.Addon()
        self.checkInterval = int(addon.getSetting('checkinterval'))
        self.numberOfChecks = int(addon.getSetting('numberofchecks'))
        self.useConstantFanPower = addon.getSetting('useconstantfanpower') != 'false'
        if self.useConstantFanPower:
            self.constantFanPower = int(addon.getSetting('constantfanpower'))
        else:
            self.thresoldMap = []
            for temp in range(80, 30, -5):
                try:
                    powerValue = int(addon.getSetting('fanpower{0}'.format(temp)))
                    self.thresoldMap.append((temp, powerValue))
                    xbmc.log("## {0} ## Mapping: {1} degree => {2} %".format(xbmcaddon.Addon().getAddonInfo('name'), temp, powerValue), level = xbmc.LOGDEBUG)
                except:
                    pass

    def getWaitTime(self, waitStart = None, now = None):
        currentTime = time.time()
        if waitStart is None:
            waitStart = currentTime
        if now is None:
            now = currentTime
        if (self.useConstantFanPower):
            return None
        else:
            waitUntil = waitStart + self.checkInterval
            if now < waitUntil:
                return waitUntil - now
            else:
                return 0

    def getFanPowerForTemp(self, temp):
        if (self.useConstantFanPower):
            return self.constantFanPower
        elif temp is None:
            return None
        for valueTuple in self.thresoldMap:
            if valueTuple[0] <= temp:
                return valueTuple[1]
        return 0


class ArgonControl(xbmc.Monitor):

    def __init__(self):
        # addon name used for logging
        self.name = xbmcaddon.Addon().getAddonInfo('name')

        # was there a notification 'System.OnQuit'?
        self.systemQuitting = False

        # is this addon stopping or stopped?
        self.stopping = False

        # did the user change the settings since last cpu temperature check?
        self.settingsChanged = True

        # time when a rising edge on power button signal was detected
        self.pwrRiseTime = None

        # load settings initially
        self.loadSettings()

        # used for synchronisation of stopAddon(), smbus writes and wait() in temperature check loop
        self.cv = threading.Condition(lock = threading.Lock())
        
         # initialize smbus for fan control
        self.i2cNumber = 0 if GPIO.RPI_INFO['P1_REVISION'] == 1 else 1
        self.smbus = smbus.SMBus(self.i2cNumber)

        # prepare values required for power cut using systemd service and install service temporary
        self.servicePath = os.path.abspath(os.path.join(xbmcaddon.Addon().getAddonInfo('path'), 'resources/argononeshutdown.service'))
        smBusPath = os.path.abspath(os.path.dirname(smbus.__file__))
        #self.shutdownCmd = 'LD_LIBRARY_PATH=\\"{0}/lib\\" \\"{0}/bin/i2cset\\" -y {1} 0x01a 0xff'.format(smBusPath, self.i2cNumber)
        self.shutdownCmd = 'LD_LIBRARY_PATH=\"{0}\" \"{0}/../bin/i2cset\" -y {1} 0x01a 0xff'.format(smBusPath, self.i2cNumber)
        self.enableShutdownService()

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

    def disableShutdownService(self):
        serviceName = os.path.basename(self.servicePath)
        result = subprocess.run(['/usr/bin/systemctl', 'disable', serviceName, '--runtime'], check = True, timeout = 5)

    def enableShutdownService(self):
        subprocess.run(['/usr/bin/systemctl', 'set-environment', 'argonone_shutdown_command={0}'.format(self.shutdownCmd), '--runtime'], check = True, timeout = 5)
        subprocess.run(['/usr/bin/systemctl', 'enable', self.servicePath, '--runtime'], check = True, timeout = 5)

    def loadSettings(self):
        self.settings = ArgonSettings()
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
  
                        # close smbus connection
                        self.smbus.close()

                        # stop systemd service?
                        if not self.systemQuitting:
                            xbmc.log("## {0} ## disabling systemd shutdown service".format(self.name), level = xbmc.LOGDEBUG)
                            self.disableShutdownService()

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
                waitStart = time.time()
                waitTime = self.settings.getWaitTime(waitStart = waitStart)
                while not self.stopping and not self.settingsChanged and (waitTime is None or waitTime > 0):
                    xbmc.log("## {0} ## wait for {1} seconds ...".format(self.name, waitTime), level = xbmc.LOGDEBUG)
                    self.cv.wait(timeout = waitTime)
                    waitTime = self.settings.getWaitTime(waitStart = waitStart)

                # check for stop
                if self.stopping:
                    xbmc.log("## {0} ## stop check was true".format(self.name), level = xbmc.LOGDEBUG)
                    break

            if self.settings.useConstantFanPower:
                # no cpu temperature required!
                cpuTemp = None
                tempCache.clear()
                xbmc.log("## {0} ## using constant fan setting so temperature check is skipped!".format(self.name), level = xbmc.LOGDEBUG)
            else :

                # get cpu temperature
                with open('/sys/class/thermal/thermal_zone0/temp', 'r') as fd:
                    cpuTemp = int(fd.read()) // 1000
                    xbmc.log("## {0} ## measured temperature - value is {1} degree celcius".format(self.name, cpuTemp), level = xbmc.LOGDEBUG)

                # save temperature value, drop oldest value(s) if necessary
                tempCache.append(cpuTemp)
                while len(tempCache) > self.settings.numberOfChecks:
                    tempCache.pop(0)

                # highest temperature value is relevant
                for tempValue in tempCache:
                    if tempValue > cpuTemp:
                        cpuTemp = tempValue
                xbmc.log("## {0} ## determined {1} degree celcius as relevant value for further processing".format(self.name, cpuTemp), level = xbmc.LOGDEBUG)
     
            # find matching setting for fan power and send to case
            fanValue = self.settings.getFanPowerForTemp(cpuTemp)
            if fanValue is None:
                xbmc.log("## {0} ## having no fan value!".format(self.name, fanValue), level = xbmc.LOGDEBUG)
            elif currentFanValue != fanValue:
                xbmc.log("## {0} ## will set fan power to {1}%".format(self.name, fanValue), level = xbmc.LOGDEBUG)
                with self.cv:
                    if self.stopping:
                        xbmc.log("## {0} ## setting fan power skipped due to module stop!".format(self.name), level = xbmc.LOGDEBUG)
                    else:
                        self.smbus.write_byte(0x1a, fanValue)
                        currentFanValue = fanValue
            else:
                xbmc.log("## {0} ## fan power is already {1}%".format(self.name, fanValue), level = xbmc.LOGDEBUG)
            
            # reset setting flag
            self.settingsChanged = False


if __name__ == '__main__':
    xbmc.log("## {0} ## sys.path: {1}".format(xbmcaddon.Addon().getAddonInfo('name'), sys.path), level = xbmc.LOGDEBUG)
    argonControl = ArgonControl()
    try:
        threading.Thread(target = argonControl.monitorCpuTemperature).start()
        argonControl.waitForAbort()
    finally:
        argonControl.stopAddon()  

