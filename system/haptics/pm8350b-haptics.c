// SPDX-License-Identifier: GPL-2.0-only
/*
 * Minimal driver for the HV haptics block (HAP520) in the Qualcomm PM8350B PMIC,
 * as used by the Nothing Phone (1). Only DIRECT_PLAY (constant amplitude) is
 * supported, exposed as a memless FF_RUMBLE input device so feedbackd picks it up.
 *
 * Register map and play sequence follow Nothing's downstream qcom-hv-haptics.c
 * (NothingOSS / LineageOS android_kernel_nothing_sm7325). The mainline device
 * tree has no haptics node, so the module borrows the PMIC's regmap through its
 * GPIO child (qcom,pm8350b-gpio) and registers its own platform device.
 */
#include <linux/delay.h>
#include <linux/input.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>
#include <linux/workqueue.h>

/* HAPTICS_CFG module */
#define CFG_BASE		0xf000
#define CFG_REVISION2		0x01
#define CFG_FAULT_STATUS	0x0c
#define CFG_EN_CTL		0x46
#define  HAPTICS_EN		BIT(7)
#define CFG_VMAX		0x48
#define  VMAX_STEP_MV		50
#define CFG_DRV_WF_SEL		0x49
#define  DRV_WF_FMT		BIT(4)
#define  DRV_WF_SEL_MASK	GENMASK(1, 0)
#define  WF_SINE		1
#define CFG_SPMI_PLAY		0x4c
#define  PLAY_EN		BIT(7)
#define  PAT_SRC_DIRECT		1
#define CFG_TLRA_OL_HIGH	0x5c
#define  TLRA_STEP_US		5
#define CFG_AUTORES_CFG		0x63
#define  AUTORES_EN		BIT(7)
#define CFG_FAULT_CLR		0x66
#define  FAULT_CLR_ALL		(BIT(2) | BIT(1) | BIT(0))
#define CFG_VSET_CFG		0x68
#define  FORCE_VREG_RDY		BIT(0)
#define CFG_CAL_EN		0x72
#define  CAL_RC_CLK_MASK	GENMASK(3, 2)
#define  CAL_RC_CLK_AUTO	(1 << 2)

/* HAPTICS_PATTERN module */
#define PTN_BASE		0xf100
#define PTN_REVISION2		0x01
#define PTN_DIRECT_PLAY		0x26

/* HAPTICS_HBOOST module */
#define HBST_BASE		0xf200

#define MAX_VMAX_MV		10000
#define MAX_PLAY_MS		5000	/* safety net: never buzz longer than this */

static unsigned int vmax_mv = 2000;
module_param(vmax_mv, uint, 0644);
MODULE_PARM_DESC(vmax_mv, "Peak drive voltage in mV at full strength (stock effects use 3600)");

static unsigned int lra_period_us = 5880;
module_param(lra_period_us, uint, 0444);
MODULE_PARM_DESC(lra_period_us, "LRA resonance period in us (stock DT: 5880)");

struct hap {
	struct device *dev;
	struct regmap *regmap;
	struct input_dev *input;
	struct work_struct work;
	struct delayed_work safety;
	struct mutex lock;
	u8 level;	/* requested amplitude, 0 = off */
	bool playing;
};

static int hap_write(struct hap *h, u16 reg, u8 val)
{
	int rc = regmap_write(h->regmap, reg, val);

	if (rc)
		dev_err(h->dev, "write %#x failed: %d\n", reg, rc);
	return rc;
}

static int hap_update(struct hap *h, u16 reg, u8 mask, u8 val)
{
	int rc = regmap_update_bits(h->regmap, reg, mask, val);

	if (rc)
		dev_err(h->dev, "update %#x failed: %d\n", reg, rc);
	return rc;
}

static void hap_stop(struct hap *h)
{
	hap_write(h, CFG_BASE + CFG_SPMI_PLAY, PAT_SRC_DIRECT);
	hap_update(h, CFG_BASE + CFG_VSET_CFG, FORCE_VREG_RDY, 0);
	h->playing = false;
}

static void hap_start(struct hap *h, u8 amplitude)
{
	unsigned int vmax = min(vmax_mv, (unsigned int)MAX_VMAX_MV);

	if (hap_write(h, CFG_BASE + CFG_FAULT_CLR, FAULT_CLR_ALL) ||
	    hap_write(h, CFG_BASE + CFG_VMAX, vmax / VMAX_STEP_MV) ||
	    hap_write(h, PTN_BASE + PTN_DIRECT_PLAY, amplitude) ||
	    hap_update(h, CFG_BASE + CFG_AUTORES_CFG, AUTORES_EN, AUTORES_EN) ||
	    hap_update(h, CFG_BASE + CFG_VSET_CFG, FORCE_VREG_RDY, FORCE_VREG_RDY) ||
	    hap_write(h, CFG_BASE + CFG_SPMI_PLAY, PAT_SRC_DIRECT | PLAY_EN)) {
		hap_stop(h);
		return;
	}
	h->playing = true;
}

static void hap_work(struct work_struct *work)
{
	struct hap *h = container_of(work, struct hap, work);

	mutex_lock(&h->lock);
	if (h->level) {
		hap_start(h, h->level);
		mod_delayed_work(system_wq, &h->safety, msecs_to_jiffies(MAX_PLAY_MS));
	} else if (h->playing) {
		hap_stop(h);
		cancel_delayed_work(&h->safety);
	}
	mutex_unlock(&h->lock);
}

static void hap_safety(struct work_struct *work)
{
	struct hap *h = container_of(to_delayed_work(work), struct hap, safety);

	mutex_lock(&h->lock);
	if (h->playing) {
		dev_warn(h->dev, "play exceeded %d ms, stopping\n", MAX_PLAY_MS);
		h->level = 0;
		hap_stop(h);
	}
	mutex_unlock(&h->lock);
}

/* memless callback: may run in atomic context, so only record and defer */
static int hap_play_effect(struct input_dev *dev, void *data, struct ff_effect *effect)
{
	struct hap *h = input_get_drvdata(dev);
	u32 mag = effect->u.rumble.strong_magnitude;

	if (!mag)
		mag = effect->u.rumble.weak_magnitude;
	h->level = mag >> 8;
	if (mag && !h->level)
		h->level = 1;
	schedule_work(&h->work);
	return 0;
}

static void hap_close(struct input_dev *dev)
{
	struct hap *h = input_get_drvdata(dev);

	cancel_work_sync(&h->work);
	cancel_delayed_work_sync(&h->safety);
	mutex_lock(&h->lock);
	h->level = 0;
	if (h->playing)
		hap_stop(h);
	mutex_unlock(&h->lock);
}

static int hap_hw_init(struct hap *h)
{
	unsigned int cfg_rev, ptn_rev, hb0, hb1, en, cal, fault;
	u32 t;
	int rc;

	rc = regmap_read(h->regmap, CFG_BASE + CFG_REVISION2, &cfg_rev) ?:
	     regmap_read(h->regmap, PTN_BASE + PTN_REVISION2, &ptn_rev) ?:
	     regmap_read(h->regmap, HBST_BASE + 0x00, &hb0) ?:
	     regmap_read(h->regmap, HBST_BASE + 0x01, &hb1) ?:
	     regmap_read(h->regmap, CFG_BASE + CFG_EN_CTL, &en) ?:
	     regmap_read(h->regmap, CFG_BASE + CFG_FAULT_STATUS, &fault);
	if (rc)
		return dev_err_probe(h->dev, rc, "reading revisions failed\n");

	dev_info(h->dev, "HAP_CFG rev %#x, HAP_PTN rev %#x, HBOOST rev %#x, EN_CTL %#x, faults %#x\n",
		 cfg_rev, ptn_rev, (hb1 << 8) | hb0, en, fault);
	/* PM8350B ships HAP_CFG/HAP_PTN v1 or v2; anything else is not this block */
	if (cfg_rev < 1 || cfg_rev > 3 || ptn_rev < 1 || ptn_rev > 3)
		return dev_err_probe(h->dev, -ENODEV, "unexpected haptics revision\n");

	/* make sure nothing is playing, then set up like the stock driver's hw_init */
	hap_stop(h);
	rc = hap_update(h, CFG_BASE + CFG_DRV_WF_SEL, DRV_WF_SEL_MASK | DRV_WF_FMT, WF_SINE) ?:
	     hap_update(h, CFG_BASE + CFG_CAL_EN, CAL_RC_CLK_MASK, CAL_RC_CLK_AUTO);
	if (rc)
		return rc;

	/* open-loop T_LRA, scaled like haptics_adjust_lra_period() for the auto RC clock */
	rc = regmap_read(h->regmap, CFG_BASE + CFG_CAL_EN, &cal);
	if (rc)
		return rc;
	t = lra_period_us;
	if ((cal & CAL_RC_CLK_MASK) == CAL_RC_CLK_AUTO)
		t = t * 1024 / 1000;
	t /= TLRA_STEP_US;
	rc = hap_write(h, CFG_BASE + CFG_TLRA_OL_HIGH, (t >> 8) & 0x0f) ?:
	     hap_write(h, CFG_BASE + CFG_TLRA_OL_HIGH + 1, t & 0xff);
	if (rc)
		return rc;

	if (!(en & HAPTICS_EN))
		rc = hap_write(h, CFG_BASE + CFG_EN_CTL, HAPTICS_EN);
	return rc;
}

static struct regmap *pm8350b_regmap(struct device **pmic_dev)
{
	struct device_node *np;
	struct platform_device *gpio;
	struct regmap *map = NULL;

	np = of_find_compatible_node(NULL, NULL, "qcom,pm8350b-gpio");
	if (!np)
		return NULL;
	gpio = of_find_device_by_node(np);
	of_node_put(np);
	if (!gpio)
		return NULL;
	if (gpio->dev.parent) {
		map = dev_get_regmap(gpio->dev.parent, NULL);
		*pmic_dev = gpio->dev.parent;
	}
	put_device(&gpio->dev);
	return map;
}

static int hap_probe(struct platform_device *pdev)
{
	struct device *pmic = NULL;
	struct hap *h;
	int rc;

	h = devm_kzalloc(&pdev->dev, sizeof(*h), GFP_KERNEL);
	if (!h)
		return -ENOMEM;
	h->dev = &pdev->dev;
	h->regmap = pm8350b_regmap(&pmic);
	if (!h->regmap)
		return dev_err_probe(h->dev, -EPROBE_DEFER, "PM8350B regmap not found\n");
	dev_info(h->dev, "using regmap of %s\n", dev_name(pmic));

	mutex_init(&h->lock);
	INIT_WORK(&h->work, hap_work);
	INIT_DELAYED_WORK(&h->safety, hap_safety);

	rc = hap_hw_init(h);
	if (rc)
		return rc;

	h->input = devm_input_allocate_device(h->dev);
	if (!h->input)
		return -ENOMEM;
	h->input->name = "pm8350b-haptics";
	h->input->close = hap_close;
	input_set_drvdata(h->input, h);
	input_set_capability(h->input, EV_FF, FF_RUMBLE);
	/* feedbackd plays calls as FF_PERIODIC; ff-memless emulates it with the rumble */
	input_set_capability(h->input, EV_FF, FF_PERIODIC);
	rc = input_ff_create_memless(h->input, NULL, hap_play_effect);
	if (rc)
		return rc;
	rc = input_register_device(h->input);
	if (rc)
		return rc;

	platform_set_drvdata(pdev, h);
	return 0;
}

static void hap_remove(struct platform_device *pdev)
{
	struct hap *h = platform_get_drvdata(pdev);

	cancel_work_sync(&h->work);
	cancel_delayed_work_sync(&h->safety);
	hap_stop(h);
}

static struct platform_driver hap_driver = {
	.probe = hap_probe,
	.remove = hap_remove,
	.driver = { .name = "pm8350b-haptics" },
};

static struct platform_device *hap_pdev;

static int __init hap_init(void)
{
	int rc = platform_driver_register(&hap_driver);

	if (rc)
		return rc;
	hap_pdev = platform_device_register_simple("pm8350b-haptics", -1, NULL, 0);
	if (IS_ERR(hap_pdev)) {
		platform_driver_unregister(&hap_driver);
		return PTR_ERR(hap_pdev);
	}
	return 0;
}

static void __exit hap_exit(void)
{
	platform_device_unregister(hap_pdev);
	platform_driver_unregister(&hap_driver);
}

module_init(hap_init);
module_exit(hap_exit);
MODULE_DESCRIPTION("Qualcomm PM8350B HV haptics (DIRECT_PLAY only)");
MODULE_LICENSE("GPL");
