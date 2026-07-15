# Mimari

## Katmanlar

`runtime_panel.py` yalnız arayüz ve kullanıcı olaylarını yönetir. Uzun işlemleri doğrudan UI thread'inde çalıştırmaz.

`Orchestrator`, başlangıçtan kapanışa kadar sıra ve hata toparlamanın tek sahibidir.

`NoxConsole`, Nox'a özgü işlemlerin sınırıdır. Klon oluşturma/kaldırma/açma burada uygulanır. Dosya kopyalama klonlama yöntemi değildir.

`ADBClient`, Android cihaz hazır olma, dosya gönderme, paket açma ve akış komutlarını yürütür.

`SelectiveProxy`, yalnız hedef hostları Charles'a yönlendirir. Diğer trafik doğrudan internete çıkar.

`FlowEngine`, JSON adımlarını sırayla çalıştırır ve durdurma sinyaline düzenli aralıklarla bakar.

## Durum makinesi

`IDLE → STOPPING_OLD_NOX → REMOVING_CLONE → COPYING_CLONE → LAUNCHING → WAITING_ANDROID → STARTING_PROXY → INSTALLING_CERTIFICATE → RUNNING_FLOW → CLEANING_UP → IDLE`

Her geçiş loglanmalı ve hata halinde `CLEANING_UP` çalıştırılmalıdır.
