/*
 * BLE power meter / smart trainer simulator for nRF52 and nRF5340 boards.
 *
 * Exposes two standard services:
 *   - Cycling Power Service (0x1818): power + crank revolutions (cadence)
 *   - Fitness Machine Service (0x1826): an indoor bike Zwift can control
 *
 * Over FTMS the app (e.g. Zwift) sends either:
 *   - simulation parameters (road grade): power rises a little on climbs
 *     and drops a little on descents (APP_UPHILL/DOWNHILL_CENTI_WKG)
 *   - a target power (ERG mode, e.g. in workouts): power holds that target
 * Changes are smoothed so power ramps rather than jumps.
 *
 * LED0 (if the board has one) blinks while advertising, on while connected.
 */
#include <stdlib.h>
#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/input/input.h>
#include <zephyr/logging/log.h>
#include <zephyr/random/random.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(power_meter, LOG_LEVEL_INF);

/* Cycling Power */
#define CPS_FEATURE_CRANK_REVS BIT(3)
#define CPM_FLAG_CRANK_REVS    BIT(5)
#define SENSOR_LOC_REAR_HUB    12

/* Fitness Machine */
#define FTMS_UUID_VAL          0x1826
#define FTMS_FEATURE_UUID_VAL  0x2ACC
#define FTMS_BIKE_DATA_VAL     0x2AD2
#define FTMS_POWER_RANGE_VAL   0x2AD8
#define FTMS_CONTROL_PT_VAL    0x2AD9
#define FTMS_STATUS_VAL        0x2ADA

#define FTMS_FEAT_CADENCE      BIT(1)
#define FTMS_FEAT_POWER        BIT(14)
#define FTMS_TARGET_POWER      BIT(3)
#define FTMS_TARGET_SIM        BIT(13)

/* Indoor Bike Data flags: speed is present when bit 0 is clear */
#define IBD_FLAG_CADENCE       BIT(2)
#define IBD_FLAG_POWER         BIT(6)

enum cp_op {
	CP_REQUEST_CONTROL = 0x00,
	CP_RESET = 0x01,
	CP_SET_TARGET_POWER = 0x05,
	CP_START = 0x07,
	CP_STOP = 0x08,
	CP_SET_SIM = 0x11,
	CP_RESPONSE = 0x80,
};

enum cp_result {
	CP_SUCCESS = 0x01,
	CP_NOT_SUPPORTED = 0x02,
	CP_INVALID = 0x03,
};

enum mode {
	MODE_SIM,
	MODE_ERG,
};

static atomic_t connections;
static atomic_t mode = ATOMIC_INIT(MODE_SIM);
static atomic_t grade_centi_pct;     /* road grade, 0.01 % units */
static atomic_t erg_watts;
static atomic_t base_watts = ATOMIC_INIT(CONFIG_APP_WATTS); /* flat-road power, buttons change it */
static bool cpm_notify;
static bool ibd_notify;

/* ---- GATT: Cycling Power Service ---- */

static void cpm_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	cpm_notify = (value == BT_GATT_CCC_NOTIFY);
}

static ssize_t read_cps_feature(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				void *buf, uint16_t len, uint16_t offset)
{
	uint8_t feature[4];

	sys_put_le32(CPS_FEATURE_CRANK_REVS, feature);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, feature, sizeof(feature));
}

static ssize_t read_location(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     void *buf, uint16_t len, uint16_t offset)
{
	uint8_t loc = SENSOR_LOC_REAR_HUB;

	return bt_gatt_attr_read(conn, attr, buf, len, offset, &loc, sizeof(loc));
}

BT_GATT_SERVICE_DEFINE(cps_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(BT_UUID_CPS_VAL)),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(BT_UUID_GATT_CPS_CPM_VAL),
			       BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
	BT_GATT_CCC(cpm_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(BT_UUID_GATT_CPS_CPF_VAL),
			       BT_GATT_CHRC_READ, BT_GATT_PERM_READ, read_cps_feature, NULL, NULL),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(BT_UUID_SENSOR_LOCATION_VAL),
			       BT_GATT_CHRC_READ, BT_GATT_PERM_READ, read_location, NULL, NULL),
);

#define CPM_ATTR (&cps_svc.attrs[1])

/* ---- GATT: Fitness Machine Service ---- */

static uint8_t cp_resp[3];
static struct bt_gatt_indicate_params cp_ind;
static atomic_t cp_ind_busy;

static void ibd_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ibd_notify = (value == BT_GATT_CCC_NOTIFY);
}

static void cp_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
}

static void status_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
}

static ssize_t read_ftms_feature(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 void *buf, uint16_t len, uint16_t offset)
{
	uint8_t feature[8];

	sys_put_le32(FTMS_FEAT_CADENCE | FTMS_FEAT_POWER, &feature[0]);
	sys_put_le32(FTMS_TARGET_POWER | FTMS_TARGET_SIM, &feature[4]);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, feature, sizeof(feature));
}

static ssize_t read_power_range(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				void *buf, uint16_t len, uint16_t offset)
{
	uint8_t range[6];

	sys_put_le16(0, &range[0]);
	sys_put_le16(CONFIG_APP_MAX_WATTS, &range[2]);
	sys_put_le16(1, &range[4]);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, range, sizeof(range));
}

static ssize_t write_control_point(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				   const void *buf, uint16_t len, uint16_t offset,
				   uint8_t flags);

BT_GATT_SERVICE_DEFINE(ftms_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(FTMS_UUID_VAL)),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(FTMS_FEATURE_UUID_VAL),
			       BT_GATT_CHRC_READ, BT_GATT_PERM_READ, read_ftms_feature, NULL, NULL),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(FTMS_BIKE_DATA_VAL),
			       BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
	BT_GATT_CCC(ibd_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(FTMS_POWER_RANGE_VAL),
			       BT_GATT_CHRC_READ, BT_GATT_PERM_READ, read_power_range, NULL, NULL),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(FTMS_CONTROL_PT_VAL),
			       BT_GATT_CHRC_WRITE | BT_GATT_CHRC_INDICATE,
			       BT_GATT_PERM_WRITE, NULL, write_control_point, NULL),
	BT_GATT_CCC(cp_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(FTMS_STATUS_VAL),
			       BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL, NULL),
	BT_GATT_CCC(status_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Characteristic declarations: indoor bike data and control point */
#define IBD_ATTR (&ftms_svc.attrs[3])
#define CP_ATTR  (&ftms_svc.attrs[8])

static void cp_ind_done(struct bt_gatt_indicate_params *params)
{
	atomic_clear(&cp_ind_busy);
}

/* Control point responses are sent as indications from the system workqueue. */
static void cp_resp_work_handler(struct k_work *work)
{
	if (!atomic_cas(&cp_ind_busy, 0, 1)) {
		return;
	}
	cp_ind.attr = CP_ATTR;
	cp_ind.data = cp_resp;
	cp_ind.len = sizeof(cp_resp);
	cp_ind.func = NULL;
	cp_ind.destroy = cp_ind_done;
	if (bt_gatt_indicate(NULL, &cp_ind)) {
		atomic_clear(&cp_ind_busy);
	}
}

static K_WORK_DEFINE(cp_resp_work, cp_resp_work_handler);

static ssize_t write_control_point(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				   const void *buf, uint16_t len, uint16_t offset,
				   uint8_t flags)
{
	const uint8_t *data = buf;
	uint8_t result = CP_SUCCESS;

	if (offset != 0 || len < 1) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	switch (data[0]) {
	case CP_REQUEST_CONTROL:
	case CP_START:
	case CP_STOP:
		break;
	case CP_RESET:
		atomic_set(&mode, MODE_SIM);
		atomic_set(&grade_centi_pct, 0);
		break;
	case CP_SET_TARGET_POWER:
		if (len < 3) {
			result = CP_INVALID;
			break;
		}
		atomic_set(&erg_watts, (int16_t)sys_get_le16(&data[1]));
		atomic_set(&mode, MODE_ERG);
		LOG_INF("ERG target %d W", (int)atomic_get(&erg_watts));
		break;
	case CP_SET_SIM:
		/* wind speed (sint16), grade (sint16, 0.01 %), crr, cw */
		if (len < 5) {
			result = CP_INVALID;
			break;
		}
		atomic_set(&grade_centi_pct, (int16_t)sys_get_le16(&data[3]));
		atomic_set(&mode, MODE_SIM);
		LOG_INF("Grade %d.%02d %%", (int)atomic_get(&grade_centi_pct) / 100,
			abs((int)atomic_get(&grade_centi_pct)) % 100);
		break;
	default:
		result = CP_NOT_SUPPORTED;
		break;
	}

	cp_resp[0] = CP_RESPONSE;
	cp_resp[1] = data[0];
	cp_resp[2] = result;
	k_work_submit(&cp_resp_work);
	return len;
}

/* ---- Advertising ---- */

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
	BT_DATA_BYTES(BT_DATA_UUID16_ALL,
		      BT_UUID_16_ENCODE(FTMS_UUID_VAL),
		      BT_UUID_16_ENCODE(BT_UUID_CPS_VAL),
		      BT_UUID_16_ENCODE(BT_UUID_DIS_VAL)),
	/* FTMS service data: machine available, indoor bike */
	BT_DATA_BYTES(BT_DATA_SVC_DATA16,
		      BT_UUID_16_ENCODE(FTMS_UUID_VAL), 0x01, 0x20, 0x00),
	BT_DATA_BYTES(BT_DATA_GAP_APPEARANCE,
		      BT_BYTES_LIST_LE16(CONFIG_BT_DEVICE_APPEARANCE)),
};

static const struct bt_data sd[] = {
	BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME,
		sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};

static void adv_work_handler(struct k_work *work)
{
	int err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_1, ad, ARRAY_SIZE(ad),
				  sd, ARRAY_SIZE(sd));

	if (err && err != -EALREADY) {
		LOG_ERR("Advertising failed to start (err %d)", err);
		return;
	}
	LOG_INF("Advertising as \"%s\"", CONFIG_BT_DEVICE_NAME);
}

static K_WORK_DEFINE(adv_work, adv_work_handler);

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		LOG_WRN("Connection failed (0x%02x)", err);
		return;
	}
	atomic_inc(&connections);
	LOG_INF("Connected");
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	atomic_dec(&connections);
	LOG_INF("Disconnected (0x%02x)", reason);
	if (atomic_get(&connections) == 0) {
		atomic_set(&mode, MODE_SIM);
		atomic_set(&grade_centi_pct, 0);
		atomic_clear(&cp_ind_busy);
	}
}

/* Restart advertising once the connection object is freed. */
static void recycled(void)
{
	k_work_submit(&adv_work);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.recycled = recycled,
};

/* ---- LED ---- */

#if DT_NODE_HAS_STATUS_OKAY(DT_ALIAS(led0))
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
#define HAS_LED 1
#endif

static void led_init(void)
{
#ifdef HAS_LED
	if (gpio_is_ready_dt(&led)) {
		gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	}
#endif
}

static void led_update(void)
{
#ifdef HAS_LED
	static bool on;

	on = atomic_get(&connections) > 0 ? true : !on;
	gpio_pin_set_dt(&led, on);
#endif
}

/* ---- Buttons ---- */

#ifdef CONFIG_INPUT
/* Runs on the input thread. gpio-keys debounces the pins for us. */
static void button_handler(struct input_event *evt, void *user_data)
{
	if (evt->type != INPUT_EV_KEY || !evt->value) {
		return; /* only act on presses, not releases */
	}

	int step;

	switch (evt->code) {
	case INPUT_KEY_0:
		step = CONFIG_APP_BUTTON_STEP_WATTS;
		break;
	case INPUT_KEY_1:
		step = -CONFIG_APP_BUTTON_STEP_WATTS;
		break;
	default:
		return;
	}

	int w = CLAMP((int)atomic_get(&base_watts) + step, CONFIG_APP_MIN_WATTS,
		      CONFIG_APP_MAX_WATTS);

	atomic_set(&base_watts, w);
	LOG_INF("Base power %d W", w);
}

INPUT_CALLBACK_DEFINE(NULL, button_handler, NULL);
#endif

/* ---- Rider model ---- */

struct crank {
	uint32_t revs;
	int64_t last_event_ms;
};

static void crank_update(struct crank *c, int64_t now_ms, int rpm)
{
	if (rpm <= 0) {
		return;
	}
	int64_t period_ms = 60000 / rpm;

	while (c->last_event_ms + period_ms <= now_ms) {
		c->last_event_ms += period_ms;
		c->revs++;
	}
}

static int jitter(int range)
{
	if (range <= 0) {
		return 0;
	}
	return (int)(sys_rand32_get() % (2 * range + 1)) - range;
}

/* Target power for the current mode, before smoothing and jitter. */
static int target_watts(void)
{
	int w;

	if (atomic_get(&mode) == MODE_ERG) {
		w = (int)atomic_get(&erg_watts);
	} else {
		/* Climbs add up to APP_UPHILL_CENTI_WKG, descents take off up to
		 * APP_DOWNHILL_CENTI_WKG, scaled by grade until it reaches
		 * APP_FULL_GRADE_CENTI_PCT.
		 */
		int grade = CLAMP((int)atomic_get(&grade_centi_pct),
				  -CONFIG_APP_FULL_GRADE_CENTI_PCT,
				  CONFIG_APP_FULL_GRADE_CENTI_PCT);
		int centi_wkg = grade >= 0 ? CONFIG_APP_UPHILL_CENTI_WKG
					   : CONFIG_APP_DOWNHILL_CENTI_WKG;

		w = (int)atomic_get(&base_watts) + (CONFIG_APP_RIDER_KG * centi_wkg * grade) /
					       (100 * CONFIG_APP_FULL_GRADE_CENTI_PCT);
	}
	return CLAMP(w, CONFIG_APP_MIN_WATTS, CONFIG_APP_MAX_WATTS);
}

/* Cadence drops a little on climbs and rises a little on descents. */
static int target_cadence(void)
{
	if (CONFIG_APP_CADENCE_RPM <= 0) {
		return 0;
	}
	int grade_pct = (int)atomic_get(&grade_centi_pct) / 100;

	if (atomic_get(&mode) == MODE_ERG) {
		grade_pct = 0;
	}
	return CLAMP(CONFIG_APP_CADENCE_RPM - grade_pct, CONFIG_APP_CADENCE_RPM - 15,
		     CONFIG_APP_CADENCE_RPM + 5);
}

/* Rough flat-road speed for a given power (km/h * 100), for Indoor Bike Data. */
static uint16_t speed_centi_kmh(int watts)
{
	/* P = 0.5 * rho * CdA * v^3 + m * g * Crr * v, solved by Newton's method */
	const float k_aero = 0.5f * 1.2f * 0.32f;
	const float k_roll = 83.0f * 9.81f * 0.004f;
	float v = 8.0f;

	for (int i = 0; i < 8; i++) {
		float f = k_aero * v * v * v + k_roll * v - (float)watts;
		float df = 3.0f * k_aero * v * v + k_roll;

		v -= f / df;
	}
	return (uint16_t)(MAX(v, 0.0f) * 3.6f * 100.0f);
}

static void send_cps(int16_t watts, const struct crank *c, bool crank_data)
{
	uint8_t buf[8];
	size_t len = 4;

	sys_put_le16(crank_data ? CPM_FLAG_CRANK_REVS : 0, &buf[0]);
	sys_put_le16((uint16_t)watts, &buf[2]);
	if (crank_data) {
		/* Last crank event time is in 1/1024 s and wraps at 16 bits. */
		sys_put_le16((uint16_t)c->revs, &buf[4]);
		sys_put_le16((uint16_t)((c->last_event_ms * 1024) / 1000), &buf[6]);
		len = 8;
	}
	bt_gatt_notify(NULL, CPM_ATTR, buf, len);
}

static void send_ibd(int16_t watts, int rpm)
{
	uint8_t buf[8];

	sys_put_le16(IBD_FLAG_CADENCE | IBD_FLAG_POWER, &buf[0]);
	sys_put_le16(speed_centi_kmh(watts), &buf[2]);
	sys_put_le16((uint16_t)(rpm * 2), &buf[4]);     /* 0.5 rpm units */
	sys_put_le16((uint16_t)watts, &buf[6]);
	bt_gatt_notify(NULL, IBD_ATTR, buf, sizeof(buf));
}

int main(void)
{
	struct crank crank = {0};
	float watts = CONFIG_APP_WATTS;
	int err;

	led_init();

	err = bt_enable(NULL);
	if (err) {
		LOG_ERR("Bluetooth init failed (err %d)", err);
		return 0;
	}
	k_work_submit(&adv_work);

	LOG_INF("Power sim: %d W base, %d kg, +0.%02d/-0.%02d W/kg on hills, %d rpm",
		CONFIG_APP_WATTS, CONFIG_APP_RIDER_KG, CONFIG_APP_UPHILL_CENTI_WKG,
		CONFIG_APP_DOWNHILL_CENTI_WKG, CONFIG_APP_CADENCE_RPM);

	while (1) {
		k_msleep(CONFIG_APP_UPDATE_MS);
		led_update();

		/* Move a share of the way to the target each update, so power ramps. */
		watts += (target_watts() - watts) * CONFIG_APP_RAMP_PCT / 100.0f;

		int rpm = target_cadence();

		if (rpm > 0) {
			rpm += jitter(2);
		}
		crank_update(&crank, k_uptime_get(), rpm);

		int out = MAX(0, (int)(watts + 0.5f) + jitter(CONFIG_APP_JITTER_WATTS));

		if (cpm_notify) {
			send_cps((int16_t)out, &crank, rpm > 0);
		}
		if (ibd_notify) {
			send_ibd((int16_t)out, rpm);
		}
	}
	return 0;
}
