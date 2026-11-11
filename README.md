# NUT_UPS – UPS Monitor plugin for Domoticz

Domoticz plugin that reads UPS status from a [NUT](https://networkupstools.org/) server.
Fork of [999LV/NUT_UPS](https://github.com/999LV/NUT_UPS) by **Logread**, unmaintained since 2019.

## 0.3.0

Works with Python 3.13+: `telnetlib` (removed in 3.13) is replaced by the standard `socket` module, with no extra
packages. Devices, their history and the plugin settings are unchanged.

## Install / update

```sh
cd <domoticz>/plugins && git clone https://github.com/arturpzol/NUT_UPS.git   # install
cd <domoticz>/plugins/NUT_UPS && git pull                                      # update
```

Then in Domoticz go to *Setup → Hardware → UPS Monitor → Update* (or restart Domoticz).

To list the variables your UPS reports: `upsc ups@127.0.0.1`

Tests: `python3 -m unittest discover -s tests`

## License

MIT, see [LICENSE](LICENSE). Original work © 2017 Logread.
