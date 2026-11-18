# Профиль Chromium

`playwright-seccomp.json` основан на полном профиле из
[Playwright bbd948344ea9](https://github.com/microsoft/playwright/blob/bbd948344ea9a60bf1e6f125c02ca99b2b51593b/utils/docker/seccomp_profile.json).
Драйвер Playwright Python 1.65.2 содержит Node Playwright 1.65.1, которому
соответствует этот commit.

SHA-256 оригинала: `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
Лицензия и уведомления: [LICENSE](PLAYWRIGHT-LICENSE), [NOTICE](PLAYWRIGHT-NOTICE).

Для T09 владелец утвердил два дополнительных вызова: `pidfd_open` и
`pidfd_send_signal`. Они нужны для адресной остановки дочерних процессов.
Все остальные правила исходного профиля сохранены. Локальный SHA-256:
`cb70791c2c826816842bd720371a0bf68cd0d57552b4f73eb92dbda9c8fbfb00`.
