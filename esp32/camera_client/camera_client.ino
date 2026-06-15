/*
 * ============================================================
 * ESP32-CAM - CAMERA CLIENT với ESP-NOW + LIVESTREAM
 * ============================================================
 * Chức năng chính:
 * 1. Nhận trigger từ ESP32 LCD qua ESP-NOW để chụp ảnh
 * 2. Upload ảnh lên server backend (FastAPI) để nhận diện
 * 3. Stream video realtime qua HTTP (MJPEG) trên port 81
 * 4. Tự động bật flash LED khi môi trường tối
 * 
 * Hardware: ESP32-CAM AI Thinker
 * 
 * Files liên quan:
 * - backend/main.py - Server nhận diện face + anti-spoofing
 * - backend/static/index.html - Web UI với livestream viewer
 * - esp32/testlcd/testlcd.ino - ESP32 LCD (gửi trigger)
 * ============================================================
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_now.h>
#include "esp_http_server.h"
#include <WiFiUdp.h>

// ==========================================
// CONFIGURATION - Cấu hình hệ thống
// ==========================================

// WiFi credentials
const char* ssid = "con meo";
const char* password = "meomeomeo";

// Static IP Configuration - Đã tắt để tự động nhận IP (DHCP)
// IPAddress local_IP(192, 168, 252, 200);      // IP cố định cho ESP32-CAM
// IPAddress gateway(192, 168, 252, 1);         // Gateway của router
// IPAddress subnet(255, 255, 255, 0);          // Subnet mask
// IPAddress primaryDNS(8, 8, 8, 8);            // Google DNS

// Backend server
String serverHost = "10.150.115.107";  // IP máy chạy backend (sẽ tự động cập nhật qua UDP Broadcast)
const int serverPort = 8080;


WiFiUDP udp;
const int udpPort = 12345;
bool serverIPDiscovered = false;

bool discoverServerIP() {
  Serial.println("[UDP] Dang do tim IP server qua UDP Broadcast...");
  udp.begin(udpPort);
  
  // Gửi gói tin Broadcast (255.255.255.255) tới port 12345
  IPAddress broadcastIP(255, 255, 255, 255);
  udp.beginPacket(broadcastIP, udpPort);
  udp.print("WHERE_IS_THE_SERVER_CAM");
  udp.endPacket();
  
  // Chờ phản hồi trong tối đa 3 giây
  unsigned long startTime = millis();
  while (millis() - startTime < 3000) {
    int packetSize = udp.parsePacket();
    if (packetSize) {
      char replyBuffer[64];
      int len = udp.read(replyBuffer, sizeof(replyBuffer) - 1);
      if (len > 0) {
        replyBuffer[len] = '\0';
        String reply = String(replyBuffer);
        reply.trim();
        if (reply == "I_AM_THE_SERVER") {
          serverHost = udp.remoteIP().toString();
          serverIPDiscovered = true;
          Serial.print("[UDP] Da tim thay server backend! IP: ");
          Serial.println(serverHost);
          udp.stop();
          return true;
        }
      }
    }
    delay(50);
  }
  
  udp.stop();
  Serial.println("[UDP] Khong tim thay server backend (Het thoi gian cho)!");
  return false;
}

// Pin definitions cho AI THINKER Model
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM     0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM       5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#define FLASH_LED_PIN     4

// State variables - Biến trạng thái
volatile bool triggerReceived = false;       // Flag nhận trigger từ ESP-NOW
bool cameraReady = false;                     // Camera đã sẵn sàng chưa
unsigned long lastCaptureTime = 0;            // Thời điểm chụp ảnh cuối
const unsigned long CAPTURE_COOLDOWN = 5000;  // Cooldown 5s giữa các lần chụp (tránh spam)

// Camera Stream Server
httpd_handle_t stream_httpd = NULL;

// ============================================================
// CAMERA STREAM SERVER - HTTP Server cho livestream
// ============================================================

/**
 * Stream Handler - Gửi MJPEG stream (ảnh liên tiếp) đến client
 * Format: multipart/x-mixed-replace (MJPEG standard)
 */
static esp_err_t stream_handler(httpd_req_t *req) {
  camera_fb_t * fb = NULL;
  esp_err_t res = ESP_OK;
  size_t _jpg_buf_len = 0;
  uint8_t * _jpg_buf = NULL;
  char * part_buf[64];

  // Set response type là MJPEG stream
  res = httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
  if(res != ESP_OK){
    return res;
  }

  // Loop vô hạn - gửi frame liên tục
  while(true){
    // Capture 1 frame từ camera
    fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("[STREAM] Camera capture failed");
      res = ESP_FAIL;
    } else {
      // Nếu không phải JPEG, convert sang JPEG
      if(fb->format != PIXFORMAT_JPEG){
        bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
        esp_camera_fb_return(fb);
        fb = NULL;
        if(!jpeg_converted){
          Serial.println("[STREAM] JPEG compression failed");
          res = ESP_FAIL;
        }
      } else {
        _jpg_buf_len = fb->len;
        _jpg_buf = fb->buf;
      }
    }
    
    // Gửi JPEG frame đến client
    if(res == ESP_OK){
      // Gửi Content-Type header cho frame này
      size_t hlen = snprintf((char *)part_buf, 64, 
        "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n", _jpg_buf_len);
      res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
    }
    if(res == ESP_OK){
      // Gửi JPEG data
      res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
    }
    if(res == ESP_OK){
      // Gửi boundary để phân tách các frame
      res = httpd_resp_send_chunk(req, "\r\n--frame\r\n", 13);
    }
    
    // Giải phóng memory
    if(fb){
      esp_camera_fb_return(fb);
      fb = NULL;
      _jpg_buf = NULL;
    } else if(_jpg_buf){
      free(_jpg_buf);
      _jpg_buf = NULL;
    }
    
    // Nếu có lỗi hoặc client disconnect, thoát loop
    if(res != ESP_OK){
      break;
    }
  }
  return res;
}

/**
 * Khởi động HTTP Server cho camera stream
 * Server chạy trên port 81
 * Endpoint: http://192.168.252.200:81/stream
 */
void startCameraServer(){
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 81;  // Port 81 (tránh conflict với port 80)

  // Đăng ký endpoint /stream
  httpd_uri_t stream_uri = {
    .uri       = "/stream",
    .method    = HTTP_GET,
    .handler   = stream_handler,
    .user_ctx  = NULL
  };
  
  // Khởi động server
  if (httpd_start(&stream_httpd, &config) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
    Serial.println("[HTTP] Camera stream server started on port 81");
  } else {
    Serial.println("[HTTP] ERROR: Failed to start camera stream server!");
  }
}

// ============================================================
// ESP-NOW - Nhận trigger từ ESP32 LCD
// ============================================================

/**
 * Callback được gọi khi nhận data từ ESP-NOW
 * ESP32 LCD sẽ gửi byte 0x01 để trigger chụp ảnh
 */
void onDataRecv(const esp_now_recv_info *recv_info, const uint8_t *data, int len) {
  if (len >= 1 && data[0] == 0x01) {  // 0x01 = Trigger command
    Serial.println("[ESP-NOW] Trigger received from LCD!");     
    triggerReceived = true;
    
    // Gửi ACK về ESP32 LCD để xác nhận đã nhận
    uint8_t ack[2] = {0xAA, 0xBB};
    esp_now_send(recv_info->src_addr, ack, sizeof(ack));
  }
}

// ============================================================
// SETUP - Khởi tạo hệ thống
// ============================================================

void setup() {
  // Serial Monitor
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n========================================");
  Serial.println("ESP32-CAM Starting...");
  Serial.println("========================================");
  
  // Flash LED setup
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);
  
  // ===== WiFi Setup với DHCP =====
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(100);
  
  // Hiển thị MAC Address (cần cho ESP-NOW pairing)
  Serial.print("[WiFi] MAC Address: ");
  Serial.println(WiFi.macAddress());
  
  // Cấu hình Static IP - Đã tắt để tự động nhận IP (DHCP)
  /*
  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS)) {
    Serial.println("[WiFi] ERROR: Static IP config failed!");
  }
  */
  
  // Kết nối WiFi
  Serial.print("[WiFi] Connecting to: ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] ✓ Connected!");
    Serial.print("[WiFi] IP: ");
    Serial.println(WiFi.localIP());
    Serial.println("[WiFi] Stream URL: http://" + WiFi.localIP().toString() + ":81/stream");
    
    // Tự động tìm IP server backend ngay sau khi kết nối WiFi thành công
    discoverServerIP();
  } else {
    Serial.println("\n[WiFi] ✗ Connection FAILED!");
    // Có thể tiếp tục với ESP-NOW nhưng không thể upload/stream
  }

  // ===== ESP-NOW Setup =====
  if (esp_now_init() != ESP_OK) {
    Serial.println("[ESP-NOW] ✗ Init failed!");
    // Blink LED 3 lần nhanh để báo lỗi
    while(1) {
      for(int i=0; i<3; i++){
        digitalWrite(FLASH_LED_PIN, HIGH);
        delay(100);
        digitalWrite(FLASH_LED_PIN, LOW);
        delay(100);
      }
      delay(1000);
    }
  }
  Serial.println("[ESP-NOW] ✓ Initialized");
  
  // Đăng ký callback nhận data
  esp_now_register_recv_cb(onDataRecv);

  // ===== Camera Setup =====
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sscb_sda = SIOD_GPIO_NUM;
  config.pin_sscb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  // Frame size tối ưu: CIF (400x296) - vừa đủ cho face recognition
  if(psramFound()){
    config.frame_size = FRAMESIZE_CIF;  // 400x296 pixels
    config.jpeg_quality = 12;            // Quality 12 (0-63, thấp hơn = tốt hơn)
    config.fb_count = 2;                 // Double buffering
    Serial.println("[Camera] PSRAM found - Using CIF 400x296");
  } else {
    config.frame_size = FRAMESIZE_CIF;
    config.jpeg_quality = 15;
    config.fb_count = 1;
    Serial.println("[Camera] No PSRAM - Using CIF 400x296");
  }

  // Khởi tạo camera
  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[Camera] ✗ Init failed: 0x%x\n", err);
    cameraReady = false;
    
    // Blink LED liên tục để báo lỗi camera
    while(1) {
      for(int i = 0; i < 3; i++) {
        digitalWrite(FLASH_LED_PIN, HIGH);
        delay(100);
        digitalWrite(FLASH_LED_PIN, LOW);
        delay(100);
      }
      delay(1000);
    }
  }
  
  Serial.println("[Camera] ✓ Initialized successfully");
  cameraReady = true;
  
  // Cải thiện chất lượng ảnh với sensor settings
  sensor_t * s = esp_camera_sensor_get();
  if (s != NULL) {
    s->set_brightness(s, 0);     // -2 to 2
    s->set_contrast(s, 0);       // -2 to 2
    s->set_saturation(s, 0);     // -2 to 2
    s->set_whitebal(s, 1);       // Auto white balance
    s->set_awb_gain(s, 1);       // Auto white balance gain
    s->set_exposure_ctrl(s, 1);  // Auto exposure
    s->set_gain_ctrl(s, 1);      // Auto gain
    s->set_hmirror(s, 0);        // Horizontal mirror: 0=off, 1=on
    s->set_vflip(s, 0);          // Vertical flip: 0=off, 1=on
  }
  
  // ===== Start Camera Stream Server =====
  startCameraServer();
  
  // Blink LED 2 lần để báo sẵn sàng
  Serial.println("========================================");
  Serial.println("✓ System Ready!");
  Serial.println("✓ Waiting for ESP-NOW trigger...");
  Serial.println("========================================");
  
  for(int i = 0; i < 2; i++) {
    digitalWrite(FLASH_LED_PIN, HIGH);
    delay(150);
    digitalWrite(FLASH_LED_PIN, LOW);
    delay(150);
  }
}

// ============================================================
// HTTP TRIGGER POLLING (Dành cho HTTP Bridge)
// ============================================================
void checkHttpTrigger() {
  static unsigned long lastPollTime = 0;
  unsigned long now = millis();
  
  // Chỉ poll mỗi 300ms
  if (now - lastPollTime < 300) {
    return;
  }
  lastPollTime = now;
  
  if (WiFi.status() != WL_CONNECTED) {
    return;
  }
  
  HTTPClient http;
  String url = "http://" + serverHost + ":" + String(serverPort) + "/api/trigger/check";
  http.begin(url);
  http.setTimeout(1500); // 1.5s timeout
  
  int httpCode = http.GET();
  if (httpCode == 200) {
    String payload = http.getString();
    
    // Parse kiểm tra: {"trigger": true}
    if (payload.indexOf("\"trigger\":true") >= 0) {
      Serial.println("[HTTP Poll] Da nhan tin hieu trigger tu server!");
      triggerReceived = true;
    }
  }
  http.end();
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
  // Kiểm tra tín hiệu trigger từ HTTP server
  checkHttpTrigger();

  // Kiểm tra điều kiện chụp ảnh:
  // 1. Có trigger từ ESP-NOW hoặc HTTP Poll
  // 2. Camera sẵn sàng
  // 3. Đã hết thời gian cooldown (tránh spam)
  
  if (triggerReceived && cameraReady) {
    unsigned long now = millis();
    
    // Check cooldown
    if (now - lastCaptureTime >= CAPTURE_COOLDOWN) {
      triggerReceived = false;  // Reset flag
      lastCaptureTime = now;    // Update thời gian
      
      Serial.println("\n[TRIGGER] Processing...");
      captureAndUpload();  // Chụp và upload ảnh
    } else {
      // Còn trong cooldown
      triggerReceived = false;
      unsigned long remaining = (CAPTURE_COOLDOWN - (now - lastCaptureTime)) / 1000;
      Serial.printf("[TRIGGER] Cooldown active, wait %lu more seconds\n", remaining);
    }
  }
  
  delay(5);  // Giảm CPU usage
}


// ============================================================
// HELPER FUNCTIONS
// ============================================================

/**
 * Kiểm tra độ sáng ảnh bằng cách lấy mẫu 100 pixel
 * Return: true nếu ảnh tối (cần bật flash)
 */
bool isImageDark(camera_fb_t *fb) {
  if (!fb || fb->len == 0) return false;
  
  uint32_t brightness = 0;
  int sampleCount = 0;
  int step = fb->len / 100;  // Lấy mẫu 100 điểm đều nhau
  
  for (size_t i = 0; i < fb->len && sampleCount < 100; i += step) {
    brightness += fb->buf[i];
    sampleCount++;
  }
  
  uint8_t avgBrightness = brightness / sampleCount;
  Serial.printf("[Camera] Average brightness: %d/255\n", avgBrightness);
  
  return avgBrightness < 60;  // Threshold: <60 = tối
}

/**
 * Chụp ảnh và upload lên backend server
 * - Tự động bật flash nếu môi trường tối
 * - Upload qua HTTP POST multipart/form-data
 * - Server sẽ nhận diện khuôn mặt và điểm danh
 */
void captureAndUpload() {
  unsigned long startTime = millis();
  
  // Chụp ảnh lần đầu để kiểm tra độ sáng
  camera_fb_t * fb = esp_camera_fb_get();
  if(!fb) {
    Serial.println("[Camera] ✗ Capture failed!");
    return;
  }
  
  // Kiểm tra độ sáng
  bool needFlash = isImageDark(fb);
  
  if (needFlash) {
    Serial.println("[Camera] Image too dark, retaking with flash...");
    
    // Trả buffer cũ
    esp_camera_fb_return(fb);
    
    // Bật flash
    digitalWrite(FLASH_LED_PIN, HIGH);
    delay(100);  // Chờ flash ổn định
    
    // Chụp lại với flash
    fb = esp_camera_fb_get();
    
    // Tắt flash
    digitalWrite(FLASH_LED_PIN, LOW);
    
    if(!fb) {
      Serial.println("[Camera] ✗ Capture with flash failed!");
      return;
    }
  }
  
  Serial.printf("[Camera] ✓ Captured: %d bytes in %lu ms\n", fb->len, millis() - startTime);
  
  // ===== Upload lên server =====
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[Upload] ✗ WiFi not connected!");
    esp_camera_fb_return(fb);
    return;
  }
  
  WiFiClient client;
  // Thử kết nối, nếu thất bại thì thử tìm lại IP server qua UDP Broadcast (phòng trường hợp server đổi IP)
  if (!client.connect(serverHost.c_str(), serverPort)) {
    Serial.println("[Upload] ✗ Failed to connect to server! Trying to re-discover...");
    if (discoverServerIP()) {
      if (!client.connect(serverHost.c_str(), serverPort)) {
        Serial.println("[Upload] ✗ Still failed to connect to discovered server!");
        esp_camera_fb_return(fb);
        return;
      }
    } else {
      esp_camera_fb_return(fb);
      return;
    }
  }
  Serial.println("[Upload] Uploading to server...");
    
    // HTTP multipart/form-data boundary
    String boundary = "----ESP32CAMBoundary1234567890";
    String head = "--" + boundary + "\r\n";
    head += "Content-Disposition: form-data; name=\"file\"; filename=\"cam.jpg\"\r\n";
    head += "Content-Type: image/jpeg\r\n\r\n";
    String tail = "\r\n--" + boundary + "--\r\n";
    
    uint32_t totalLen = head.length() + fb->len + tail.length();
    
    // HTTP Request Headers
    client.println("POST /api/recognize HTTP/1.1");
    client.println("Host: " + String(serverHost));
    client.println("Content-Length: " + String(totalLen));
    client.println("Content-Type: multipart/form-data; boundary=" + boundary);
    client.println("Connection: close");
    client.println();
    
    // HTTP Request Body
    client.print(head);
    
    // Gửi image data theo chunks (2KB mỗi lần)
    uint8_t *fbBuf = fb->buf;
    size_t fbLen = fb->len;
    size_t chunkSize = 2048;
    
    for (size_t i = 0; i < fbLen; i += chunkSize) {
      size_t toSend = (fbLen - i < chunkSize) ? (fbLen - i) : chunkSize;
      size_t sent = client.write(fbBuf + i, toSend);
      
      if (sent != toSend) {
        Serial.println("[Upload] ✗ Connection error!");
        break;
      }
    }
    
    client.print(tail);
    
    // Đọc response từ server
    Serial.println("[Upload] Waiting for server response...");
    unsigned long timeout = millis();
    bool headersPassed = false;
    String response = "";
    
    while (client.connected() && millis() - timeout < 8000) {
      if (client.available()) {
        String line = client.readStringUntil('\n');
        
        if (!headersPassed) {
          if (line == "\r") {
            headersPassed = true;
          } else if (line.startsWith("HTTP/1.1")) {
            Serial.println("[Server] " + line);
          }
        } else {
          response += line;
        }
        
        timeout = millis();
      }
    }
    
    if (response.length() > 0) {
      Serial.println("[Server] Response: " + response);
    }
    
    client.stop();
    Serial.printf("[Upload] ✓ Completed in %lu ms\n", millis() - startTime);
  
  // Giải phóng memory
  esp_camera_fb_return(fb);
}
