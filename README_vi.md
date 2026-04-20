# Blueprints Updater cho Home Assistant

[![Release](https://img.shields.io/github/v/release/luuquangvu/blueprints-updater?style=flat&logo=github&label=Release&color=purple)](https://github.com/luuquangvu/blueprints-updater/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat&logo=homeassistantcommunitystore&label=HACS)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/homeassistant-%3E%3D%202024.12.0-03a9f4?style=flat&logo=homeassistant&label=Home+Assistant)](https://www.home-assistant.io)

[![CI](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/ci.yaml?style=flat&logo=github&label=CI)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/ci.yaml)
[![Validation](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/validation.yaml?style=flat&logo=github&label=Validation)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/validation.yaml)
[![CodeQL](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/github-code-scanning/codeql?style=flat&logo=github&label=CodeQL)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/github-code-scanning/codeql)
[![Prettier](https://img.shields.io/github/actions/workflow/status/luuquangvu/blueprints-updater/prettier.yaml?style=flat&logo=prettier&label=Prettier)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/prettier.yaml)

**[ [🇺🇸 English](README.md) | 🇻🇳 Tiếng Việt ]**

**Blueprints Updater** giúp các bản thiết kế (blueprint) trên Home Assistant luôn duy trì ở phiên bản mới nhất thông qua cơ chế tự động theo dõi và cập nhật (hỗ trợ Automations, Scripts và Template Entities). Tiện ích tích hợp sâu như một thực thể cập nhật gốc, cho phép bạn nâng cấp toàn bộ hệ thống chỉ với một cú nhấp chuột ngay trên bảng điều khiển.

---

## Tính năng chính

- **Tích hợp sâu như tính năng hệ thống**: Hoạt động mượt mà và đồng bộ như các bản cập nhật chính thức. Bạn có thể dễ dàng quản lý và cập nhật hàng loạt blueprint ngay trên dashboard.
- **Lớp bảo vệ Nâng cao (Advanced Compatibility Guard)**: Chủ động bảo vệ hệ thống khỏi các thay đổi gây lỗi (breaking changes). Tiện ích thực hiện kiểm tra chéo chuyên sâu giữa blueprint mới và toàn bộ automation/script hiện có trước khi cập nhật, giúp nhận diện và cảnh báo sớm các "Lỗi tương thích" (compatibility errors).
- **Tự động hóa hoàn toàn**: Khi bật chế độ tự động cập nhật, hệ thống sẽ thay bạn thực hiện mọi thao tác từ sao lưu, tải bản mới đến gửi thông báo chi tiết khi hoàn tất.
- **Tối ưu hiệu suất và băng thông**: Sử dụng mã băm SHA256 và ETag để chỉ tải về khi thực sự có thay đổi từ nguồn, giảm thiểu tải cho hệ thống.
- **Hỗ trợ Blueprint đa nền tảng**: Tương thích hoàn hảo với các tệp nguồn từ GitHub, GitHub Gist và Diễn đàn cộng đồng Home Assistant.
- **Tối ưu hóa với jsDelivr CDN**: Tận dụng mạng lưới [jsDelivr](https://www.jsdelivr.com/) CDN để tăng tốc độ tải và giảm ảnh hưởng từ giới hạn truy cập (rate limit). Cơ chế dự phòng đi kèm sẽ tự động lấy dữ liệu trực tiếp từ GitHub nếu CDN gặp sự cố, đảm bảo quá trình cập nhật luôn diễn ra thông suốt.
- **Nắm rõ tác động trước khi cập nhật**: Hiển thị chính xác số lượng Automation hoặc Script đang sử dụng blueprint đó, giúp bạn chủ động kiểm soát mọi thay đổi.
- **Kiểm soát linh hoạt theo nhu cầu**: Cho phép theo dõi toàn bộ hoặc lọc danh sách blueprint theo Whitelist/Blacklist một cách chi tiết.
- **Cập nhật tức thì, không cần khởi động lại**: Thao tác cập nhật tự động làm mới các thành phần liên quan, giúp thay đổi có hiệu lực ngay lập tức.
- **Duy trì liên kết nguồn định danh**: Tự động bảo tồn thông tin `source_url` trong tệp YAML, đảm bảo khả năng theo dõi và cập nhật lâu dài.
- **Lớp bảo mật vững chắc**: Tự động kiểm tra an toàn đường dẫn và URL, ngăn chặn các truy cập trái phép và đảm bảo tệp tin luôn nằm đúng vị trí.
- **Phát hiện tức thì, không cần chờ đợi**: Tự động nhận diện blueprint mới mà không cần khởi động lại hệ thống. Giao diện đa ngôn ngữ, tự động thích ứng với cài đặt cá nhân của bạn.

---

## Cài đặt

### Cách 1: Sử dụng HACS (Khuyên dùng)

[![Add Blueprints Updater to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=luuquangvu&repository=blueprints-updater&category=integration)

1.  Mở **HACS** trong Home Assistant.
2.  Tìm kiếm **Blueprints Updater**.
3.  Nếu không tìm thấy, nhấp vào biểu tượng ba chấm ở góc trên cùng bên phải và chọn **Kho lưu trữ tùy chỉnh (Custom repositories)**.
4.  Thêm `https://github.com/luuquangvu/blueprints-updater` với danh mục **Bộ tích hợp (Integration)**.
5.  Tìm kiếm **Blueprints Updater** và nhấp vào **Tải xuống (Download)**.
6.  Khởi động lại Home Assistant.

### Cách 2: Cài đặt thủ công

1.  Tải bản phát hành mới nhất và giải nén các tệp.
2.  Sao chép thư mục `custom_components/blueprints_updater` vào thư mục `config/custom_components/` của Home Assistant.
3.  Khởi động lại Home Assistant.

---

## Hướng dẫn thiết lập & Cấu hình

1.  Đi tới **Cài đặt (Settings)** > **Thiết bị & Dịch vụ (Devices & Services)**.
2.  Nhấp vào **Thêm bộ tích hợp (Add Integration)** và tìm kiếm **Blueprints Updater**.
3.  **Bật Tự động Cập nhật**: (Tùy chọn) Nếu được bật, các blueprint phù hợp với tiêu chí của bạn sẽ được cập nhật tự động mà không cần can thiệp thủ công. **Một thông báo hệ thống sẽ xuất hiện** sau mỗi lần tự động cập nhật thành công để bạn biết những blueprint nào đã được cập nhật.
4.  Chọn **Khoảng thời gian cập nhật (Update Interval)** (mặc định là 24 giờ).
5.  Chọn **Chế độ lọc (Filter Mode)**:
    - **Cập nhật tất cả (Update All)**: Theo dõi tất cả blueprint tìm thấy trong thư mục của bạn.
    - **Danh sách trắng (Whitelist)**: Chỉ theo dõi các blueprint bạn chọn cụ thể từ danh sách.
    - **Danh sách đen (Blacklist)**: Theo dõi tất cả các blueprint _ngoại trừ_ những cái bạn chọn.
6.  **Sử dụng jsDelivr CDN**: (Tùy chọn, mặc định là bật) Bật tính năng này để sử dụng CDN jsDelivr khi tải các blueprint từ GitHub, giúp tăng hiệu suất. Nếu CDN không thể truy cập hoặc trả về lỗi, hệ thống sẽ tự động sử dụng link GitHub gốc để đảm bảo việc cập nhật thành công.
7.  Sau khi thêm, tích hợp sẽ quét các blueprint của bạn. Nếu tìm thấy bản cập nhật, chúng sẽ xuất hiện dưới dạng thực thể `update` trong bảng điều khiển của bạn.

### Sao lưu & Phục hồi (Backup & Restore)

Blueprints Updater cung cấp một mạng lưới an toàn tích hợp sẵn, cho phép bạn sao lưu các blueprint trước khi chúng được cập nhật và khôi phục chúng nếu cần thiết.

#### Kích hoạt Sao lưu

Khi cài đặt bản cập nhật từ bảng điều khiển Home Assistant, bạn sẽ có tùy chọn tích chọn **Backup (Sao lưu)**. Nếu được bật, tiện ích sẽ tự động lưu blueprint hiện tại của bạn thành một tệp sao lưu có đánh số (`.bak.1`, `.bak.2`, v.v.) trước khi ghi đè bằng phiên bản mới.

> **Lưu ý:** Nếu bạn đang bật tùy chọn **Tự động Cập nhật (Auto-Update)**, tiện ích sẽ **luôn luôn** tự động sao lưu cấu hình trước khi ghi đè bản mới nhất, tạo ra một mạng lưới an toàn 100% giúp bạn hoàn toàn yên tâm.

#### Khôi phục bản Sao lưu

Nếu bạn phát hiện ra rằng bản blueprint mới cập nhật làm hỏng các automations hoặc có thay đổi không tương thích, bạn có thể dễ dàng quay về phiên bản trước đó:

1. Đi tới **Công cụ nhà phát triển (Developer Tools)** > **Hành động (Actions)**. _Lưu ý: Yêu cầu quyền quản trị._
2. Tìm kiếm hành động **`blueprints_updater.restore_blueprint`**.
3. Chọn thực thể `update` tương ứng với blueprint mà bạn muốn khôi phục.
4. (Tùy chọn) Nhập **Backup Version** mà bạn muốn khôi phục (mặc định là **1** cho bản sao lưu gần nhất).
5. Nhấn **Thực hiện hành động (Perform Action)**.

Tiện ích sẽ tự động tìm tệp sao lưu tương ứng, khôi phục lại nội dung YAML gốc, và tự động tải lại (reload) các automations và scripts để áp dụng các thay đổi ngay lập tức.

### Lớp bảo vệ Nâng cao (Advanced Compatibility Guard)

**Advanced Compatibility Guard** là một lớp bảo mật chuyên nghiệp được thiết kế để bảo vệ logic ngôi nhà thông minh của bạn khỏi các thay đổi gây lỗi (breaking changes) trong các bản cập nhật blueprint.

Khi phát hiện bản cập nhật, hệ thống sẽ thực hiện quy trình kiểm tra an toàn gồm nhiều bước:

1.  **Kiểm tra cấu trúc**: Tự động xác thực nội dung của blueprint mới để đảm bảo nó tuân thủ các quy tắc của Home Assistant.
2.  **Phân tích tác động**: Giả lập việc cập nhật trên các tự động hóa (automations) hiện có của bạn để xem có thành phần nào bị hỏng hay không.
3.  **Cảnh báo rủi ro**: Nếu phát hiện vấn đề (chẳng hạn như thiếu thông số bắt buộc), bản cập nhật sẽ bị đánh dấu trạng thái **"Lỗi tương thích (compatibility error)"**.
4.  **Bảo vệ cập nhật tự động**: Các blueprint có nguy cơ gây lỗi sẽ bị chặn cập nhật tự động (**"blocked-auto-update"**) nhằm bảo vệ ngôi nhà của bạn khỏi các sự cố không đáng có.
5.  **Minh bạch thay đổi**: Đối với các bản cập nhật bị chặn, bạn có thể xem **"BÁO CÁO RỦI RO CẬP NHẬT (UPDATE RISK REPORT)"** để biết các thay đổi gây lỗi cụ thể (như thiếu tham số bắt buộc) và xem chi tiết phần **"Git Diff"** để so sánh mã nguồn trước khi quyết định cài đặt thủ công.

### Làm mới danh sách Blueprint

Vì Home Assistant không liên tục giám sát tệp hệ thống để tiết kiệm tài nguyên, nên **việc thêm hoặc xóa blueprint sẽ không được cập nhật ngay lập tức**.

Để áp dụng các thay đổi này tức thì mà không cần chờ đến lần quét tự động tiếp theo, bạn phải làm **một** trong các cách sau:

1. **Chạy hành động Reload (Khuyên dùng)**: Vào **Công cụ nhà phát triển (Developer Tools)** > **YAML**, tìm **Blueprints Updater** trong danh sách **YAML configuration reloading** và nhấn **Reload**. Hoặc sử dụng hành động **`blueprints_updater.reload`** trong phần **Hành động (Actions)** (Chỉ dành cho quản trị viên).
2. **Reload Tích hợp**: Vào **Cài đặt (Settings)** > **Thiết bị & Dịch vụ (Devices & Services)** > **Blueprints Updater**, nhấn vào ba chấm và chọn **Tải lại (Reload)**.
3. **Khởi động lại Home Assistant**.

### Xem thử ngay!

Nếu bạn muốn thấy quy trình cập nhật hoạt động như thế nào ngay lập tức, bạn có thể sử dụng bản thiết kế **Motion-Activated Light/Switch (Frequent Updates)**. Bản thiết kế này được cập nhật tự động thường xuyên thông qua GitHub Actions để mô phỏng một bản phát hành mới.

**Cài đặt nhanh:**
[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fluuquangvu%2Fdemo-blueprints%2Fblob%2Fmain%2Fblueprints%2Fmotion_light_blueprint.yaml)

**Cài đặt thủ công:**

1.  Sao chép URL này: `https://github.com/luuquangvu/demo-blueprints/blob/main/blueprints/motion_light_blueprint.yaml`
2.  Trong Home Assistant, đi tới **Cài đặt** > **Tự động hóa & Cảnh** > **Bản thiết kế**.
3.  Nhấp vào **Nhập bản thiết kế** và dán URL vào.

Sau khi nhập xong, **Blueprints Updater** sẽ tự động phát hiện nó trong lần quét định kỳ tiếp theo. Để thấy kết quả ngay lập tức, bạn có thể [kích hoạt quét thủ công](#làm-mới-danh-sách-blueprint). Khi GitHub Action cập nhật bản thiết kế, bạn sẽ nhận được thông báo trong Home Assistant và có thể thực hiện cập nhật.

---

## Yêu cầu

Để một blueprint có thể theo dõi được, nó **phải** chứa một `source_url` hợp lệ trong siêu dữ liệu:

```yaml
blueprint:
  name: "Tên Blueprint"
  source_url: https://github.com/user/repo/blob/main/blueprint.yaml
  # ...
```

---

## Chất lượng Mã nguồn & Bảo mật

Để duy trì tiêu chuẩn cao về độ tin cậy và an toàn, dự án sử dụng bộ công cụ phát triển và bảo mật tự động hiện đại:

- **Đánh giá Pull Request tự động**: Sử dụng [CodeRabbit](https://coderabbit.ai) để phân tích chi tiết các thay đổi, giúp phát hiện sớm các lỗi logic và trường hợp biên trước khi phát hành.
- **Tối ưu hóa mã nguồn**: [Sourcery](https://sourcery.ai) liên tục rà soát mã nguồn để đề xuất các cấu trúc Python sạch, hiệu quả và chuẩn mực hơn.
- **Phân tích tĩnh & Bảo mật**: [CodeQL](https://codeql.github.com) thực hiện quét chuyên sâu để nhận diện các rủi ro bảo mật tiềm ẩn, đảm bảo mã nguồn tuân thủ các quy chuẩn an toàn.
- **Quy trình phát triển chặt chẽ**:
  - **[Ruff](https://github.com/astral-sh/ruff)**: Kiểm tra lỗi và định dạng mã cực nhanh, giúp code luôn nhất quán.
  - **[Ty](https://github.com/astral-sh/ty)** & **[Pyright](https://github.com/Microsoft/pyright)**: Kiểm tra kiểu dữ liệu để tăng độ ổn định cho lõi hệ thống.
  - **[Pytest](https://github.com/pytest-dev/pytest)**: Hệ thống kiểm thử tự động đảm bảo các tính năng luôn vận hành ổn định.
  - **[Prettier](https://github.com/prettier/prettier)**: Duy trì định dạng nhất quán cho các tệp tài liệu và cấu hình.

> [!NOTE]
> Mọi kết quả từ các công cụ tự động đều được quản trị viên dự án trực tiếp rà soát và xác nhận kỹ lưỡng, đảm bảo sự ổn định cao nhất cho người dùng.

## Đóng góp

Sự đóng góp từ cộng đồng là yếu tố cốt lõi giúp các dự án mã nguồn mở trở nên tốt đẹp hơn. Mọi đóng góp của bạn đều được **ghi nhận và trân trọng**.

- **Nếu bạn tìm thấy lỗi hoặc sự cố**, hãy giúp dự án hoàn thiện hơn bằng cách [mở một issue](https://github.com/luuquangvu/blueprints-updater/issues).
- **Nếu bạn muốn đóng góp mã nguồn**, hãy Fork kho lưu trữ và tạo Pull Request (đừng quên kiểm tra mã nguồn theo [tiêu chuẩn chung](#chất-lượng-mã-nguồn--bảo-mật) phía trên nhé).

## Bản quyền

Dự án được phát hành dưới **Giấy phép MIT**. Xem tệp [LICENSE](LICENSE) để biết thêm thông tin chi tiết.
