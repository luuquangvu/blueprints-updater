# Blueprints Updater cho Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/luuquangvu/blueprints-updater?logo=github&style=flat&color=purple&label=GitHub+Release)](https://github.com/luuquangvu/blueprints-updater/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat&label=HACS)](https://github.com/hacs/integration)
[![Home Assistant Version](https://img.shields.io/badge/homeassistant-%3E%3D%202024.12.0-03a9f4?logo=homeassistant&style=flat&label=Home+Assistant)](https://github.com/home-assistant/core/releases/tag/2024.12.0)

[![Python Checks](https://github.com/luuquangvu/blueprints-updater/actions/workflows/python_check.yaml/badge.svg)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/python_check.yaml)
[![Validate](https://github.com/luuquangvu/blueprints-updater/actions/workflows/validate.yaml/badge.svg)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/validate.yaml)
[![CodeQL](https://github.com/luuquangvu/blueprints-updater/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/luuquangvu/blueprints-updater/actions/workflows/github-code-scanning/codeql)
[![CodeRabbit Reviews](https://img.shields.io/coderabbit/prs/github/luuquangvu/blueprints-updater?logo=github&style=flat&color=orange&label=CodeRabbit+Reviews)](https://github.com/luuquangvu/blueprints-updater/pulls)

**[ [🇺🇸 English](README.md) | 🇻🇳 Tiếng Việt ]**

**Blueprints Updater** giúp hệ thống Home Assistant của bạn luôn được làm mới bằng cách tự động theo dõi và cập nhật các blueprint (Automations, Scripts, và Template Entities). Cài đặt một lần, mọi thứ sẽ hoạt động mượt mà như một tính năng có sẵn, giúp bạn yên tâm cập nhật chỉ với một cú chạm ngay trên dashboard.

---

## Tính năng chính

- **Trải nghiệm như hàng chính chủ**: Tích hợp mượt mà vào Home Assistant, hoạt động y hệt các bản cập nhật hệ thống hay HACS. Bạn sẽ thấy các thực thể cập nhật hiện ngay trên dashboard, dễ dàng cập nhật từng cái hoặc tất cả cùng lúc chỉ với một cú nhấp chuột.
- **An toàn tuyệt đối, tự động sao lưu**: Mỗi bản cập nhật đều được xác thực kỹ lưỡng về cú pháp YAML và độ tương thích phiên bản trước khi áp dụng. Ngoài việc tự động lưu lại các bản sao lưu xoay vòng để khôi phục nhanh, cơ chế cập nhật còn được thiết kế cực kỳ an toàn, đảm bảo file của bạn không bao giờ bị hỏng kể cả khi gặp sự cố bất ngờ (như mất điện) giữa chừng.
- **Cài đặt một lần, dùng mãi mãi**: Khi bật chế độ tự động cập nhật, bạn có thể hoàn toàn rảnh tay. Hệ thống sẽ tự sao lưu bản đang dùng, tải bản mới nhất và gửi thông báo cho biết chính xác những blueprint nào vừa được cập nhật.
- **Phát hiện thay đổi thông minh**: Thay vì tải tệp tin liên tục, công cụ này sử dụng mã băm SHA256 và tiêu đề ETag để chỉ tải về khi thực sự có thay đổi. Cách làm này giúp tiết kiệm băng thông và giữ cho hệ thống luôn vận hành nhẹ nhàng.
- **Hỗ trợ nhiều nguồn khác nhau**: Dù blueprint nằm trên GitHub, Gist hay được chia sẻ trong các bài viết trên Diễn đàn cộng đồng Home Assistant, trình cập nhật này đều có khả năng xử lý chính xác và tin cậy.
- **Biết rõ tác động trước khi thực hiện**: Hệ thống hiển thị số lượng Automation hoặc Script đang sử dụng blueprint đó, giúp bạn đánh giá nhanh tác động của bản cập nhật đối với hệ thống nhà thông minh của mình.
- **Kiểm soát hoàn toàn theo ý muốn**: Bạn có quyền chọn theo dõi tất cả blueprint hoặc sử dụng danh sách Whitelist/Blacklist để chỉ tập trung vào những mục thực sự quan trọng.
- **Cập nhật tức thì, không cần khởi động lại**: Các miền liên quan (automation, script hoặc template) sẽ tự động được làm mới ngay sau khi cập nhật. Mọi thay đổi có hiệu lực tức thì mà không làm gián đoạn hoạt động của Home Assistant.
- **Giữ link nguồn vĩnh viễn**: Tự động bảo tồn các thẻ thông tin link gốc trong tệp YAML, đảm bảo blueprint của bạn luôn giữ được kết nối để nhận các bản cập nhật trong tương lai.
- **Bảo mật tích hợp sẵn**: Tự động kiểm tra an toàn đường dẫn (Path Safety) và URL để bảo vệ mạng nội bộ của bạn khỏi các truy cập trái phép, đồng thời đảm bảo mọi tệp tin blueprint luôn nằm đúng vị trí an toàn.
- **Khởi động siêu tốc và tự động nhận diện**: Blueprint mới thêm vào sẽ được nhận ngay mà không cần khởi động lại. Danh sách blueprint cũng sẽ hiển thị ngay lập tức mỗi khi bạn mở Home Assistant thay vì phải chờ đợi hệ thống quét mạng chậm chạp. Giao diện cũng hỗ trợ nhiều ngôn ngữ và tự động điều chỉnh theo cài đặt của bạn.

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
6.  Sau khi thêm, tích hợp sẽ quét các blueprint của bạn. Nếu tìm thấy bản cập nhật, chúng sẽ xuất hiện dưới dạng thực thể `update` trong bảng điều khiển của bạn.

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

## Đóng góp

Mọi đóng góp đều được chào đón! Nếu bạn tìm thấy lỗi hoặc có yêu cầu tính năng, vui lòng tạo issue hoặc gửi pull request.

## Bản quyền

Dự án này được cấp phép theo Giấy phép MIT.
