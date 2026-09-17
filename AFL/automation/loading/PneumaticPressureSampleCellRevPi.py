"""RevPi and LabJack configuration for :class:`PneumaticPressureSampleCell`.

Run this module with the standard AFL launcher. Its custom configuration uses
the LabJack for analog pressure control and two optical sensors, while the
RevPi operates valve outputs and reads the safety interlock inputs.
"""

from AFL.automation.loading.PneumaticPressureSampleCell import (
    PneumaticPressureSampleCell,
)


class PneumaticPressureSampleCellRevPi(PneumaticPressureSampleCell):
    """Pneumatic pressure sample cell configured for RevPi digital I/O."""


_DEFAULT_CUSTOM_CONFIG = {
    '_classname': (
        'AFL.automation.loading.PneumaticPressureSampleCellRevPi.'
        'PneumaticPressureSampleCellRevPi'
    ),
    '_args': [
        {
            '_classname': (
                'AFL.automation.loading.DigitalOutPressureController.'
                'DigitalOutPressureController'
            ),
            '_args': [
                {
                    '_classname': 'AFL.automation.loading.LabJackDigitalOut.LabJackDigitalOut',
                    'intermittent_device_handle': False,
                    'port_to_write': 'TDAC4',
                },
                3,
            ],
        },
        {
            '_classname': 'AFL.automation.loading.RevPiRelay.RevPiRelay',
            '_args': [{
                'O_1': 'blow', 'O_2': 'rinse1', 'O_3': 'rinse2',
                'O_4': 'enable', 'O_7': 'arm-up', 'O_8': 'arm-down',
                'O_14': 'postsample', 'O_13': 'piston-vent'
            }],
        },
    ],
    'digitalin': {
        '_classname': 'AFL.automation.loading.RevPiGPIO.RevPiGPIO',
        '_args': [{'I_1': 'ARM_UP', 'I_2': 'ARM_DOWN'}],
    },
    'robot_interlock_host': '<robot-host-or-IP>',
    'load_stopper': [
        {
            '_classname': 'AFL.automation.loading.LoadStopperDriver.LoadStopperDriver',
            '_args': [{
                '_classname': 'AFL.automation.loading.LabJackSensor.LabJackSensor',
                'port_to_read': 'AIN0',
                'reset_port': 'DIO6',
            }],
            '_add_data': 'data',
            'name': 'LoadStopperDriver_before_target',
            'auto_initialize': False,
            'sensorlabel': 'beforeTarget',
        },
        {
            '_classname': 'AFL.automation.loading.LoadStopperDriver.LoadStopperDriver',
            '_args': [{
                '_classname': 'AFL.automation.loading.LabJackSensor.LabJackSensor',
                'port_to_read': 'AIN1',
                'reset_port': 'DIO7',
            }],
            '_add_data': 'data',
            'name': 'LoadStopperDriver_after_target',
            'auto_initialize': False,
            'sensorlabel': 'afterTarget',
        },
    ],
}


if __name__ == '__main__':
    from AFL.automation.shared.launcher import *