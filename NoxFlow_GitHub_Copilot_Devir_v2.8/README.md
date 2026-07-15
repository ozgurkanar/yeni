# NoxFlow Windows Runtime

NoxFlow; NoxPlayer çalışma klonunu, Charles Proxy'yi, sertifikayı ve kayıtlı NoxFlow akışını tek yaşam döngüsünde yöneten Windows masaüstü uygulamasıdır.

## Teslimat biçimi

Son kullanıcı Python kurmaz. Uygulama GitHub Actions veya `build_exe.bat` ile Windows EXE olarak üretilir:

- `dist/NoxFlow/NoxFlow.exe`
- `dist/NoxFlow_Windows_x64.zip`

> GitHub Copilot'un projeyi geliştirebilmesi için kaynak kod depoda tutulur. Kaynak kodu silip yalnız EXE yüklemek, Copilot'un hataları düzeltmesini veya özellik eklemesini engeller.

## Hızlı başlangıç — GitHub

1. GitHub'da boş ve tercihen **Private** bir depo oluşturun.
2. Bu klasörün içeriğini deponun köküne yükleyin.
3. GitHub Desktop veya terminal ile `main` dalına gönderin.
4. GitHub'da **Actions → Windows EXE Build → Run workflow** seçin.
5. İş bitince **Artifacts → NoxFlow-Windows-x64** paketini indirin.

## Yerel Windows derlemesi

`build_exe.bat` dosyasını çalıştırın. Python 3.11/3.12 yalnız geliştirici bilgisayarında gerekir. Son kullanıcı yalnız üretilen EXE klasörünü kullanır.

## Copilot ile devam

GitHub Copilot Chat'e önce şunu yazın:

> `.github/copilot-instructions.md`, `PROJE_DEVIR.md` ve `docs/MIMARI.md` dosyalarını tamamen oku. Mevcut yaşam döngüsünü bozmadan projeyi analiz et. Önce sorunları ve değiştireceğin dosyaları yaz, ardından test ekleyerek düzelt. Teslimatta Windows EXE build'inin geçtiğini doğrula.`

## Güvenlik

Sertifika ve otomasyon akışları özel proje verisi olabilir. Depoyu public yapmadan önce içerikleri kontrol edin. Varsayılan öneri private depodur.
