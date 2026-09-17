from AFL.automation.loading.PneumaticPressureSampleCellRevPi import _DEFAULT_CUSTOM_CONFIG


def test_default_config_uses_labjack_and_revpi_io():
    pressure_controller, relayboard = _DEFAULT_CUSTOM_CONFIG['_args']

    assert pressure_controller['_args'][0]['port_to_write'] == 'TDAC4'
    assert relayboard['_classname'].endswith('RevPiRelay.RevPiRelay')
    assert relayboard['_args'][0] == {
        'O_1': 'arm-up', 'O_2': 'arm-down', 'O_3': 'rinse1',
        'O_4': 'rinse2', 'O_5': 'blow', 'O_6': 'piston-vent',
        'O_7': 'postsample',
    }
    assert _DEFAULT_CUSTOM_CONFIG['digitalin']['_args'][0] == {
        'I_1': 'DOOR', 'I_2': 'ARM_UP', 'I_3': 'ARM_DOWN',
    }


def test_default_config_provides_before_and_after_labjack_sensors():
    stoppers = _DEFAULT_CUSTOM_CONFIG['load_stopper']

    assert [stopper['_args'][0]['port_to_read'] for stopper in stoppers] == ['AIN0', 'AIN1']
    assert [stopper['sensorlabel'] for stopper in stoppers] == [
        'beforeTarget', 'afterTarget',
    ]