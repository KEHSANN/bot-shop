"""Idempotent database seed for 12-language CMS, strict 5-button menu, settings, and sample catalog."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.constants import (
    DeliveryType,
    MAIN_MENU_BUTTON_KEYS,
    MenuScope,
    SettingKey,
)
from app.core.logging import configure_logging, get_logger
from app.database.models import (
    AppSetting,
    Category,
    Product,
    ProductBulkTier,
    UIButton,
    UITranslation,
)
from app.database.session import close_db, get_session_factory, init_db
from app.locales.seed_translations import TRANSLATIONS, get_seed_translation_rows

logger = get_logger(__name__)

_MAIN_BUTTON_DEFAULTS: dict[str, tuple[int, int, str, str]] = {
    "shop": (0, 0, "🛒", "#3b82f6"),
    "deposit": (1, 0, "💳", "#22c55e"),
    "balance": (1, 1, "💰", "#eab308"),
    "support": (2, 0, "🆘", "#f97316"),
    "language": (2, 1, "🌐", "#a855f7"),
}


async def seed_translations(session: AsyncSession) -> int:
    """Insert missing (key, lang_code) translation rows across all 12 languages."""
    existing = {
        (row.key, row.lang_code)
        for row in (await session.execute(select(UITranslation.key, UITranslation.lang_code))).all()
    }
    inserted = 0
    for key, lang_code, text_value in get_seed_translation_rows():
        if (key, lang_code) not in existing:
            session.add(UITranslation(key=key, lang_code=lang_code, text_value=text_value))
            inserted += 1
    return inserted


async def seed_ui_buttons(session: AsyncSession) -> int:
    """Insert strictly scoped 5-button Main Menu layout if not already present."""
    existing = set((await session.execute(select(UIButton.button_key))).scalars().all())
    inserted = 0
    for button_key in MAIN_MENU_BUTTON_KEYS:
        if button_key in existing:
            continue
        row_idx, col_idx, emoji, hex_color = _MAIN_BUTTON_DEFAULTS[button_key]
        labels = dict(TRANSLATIONS.get(f"btn.{button_key}", {"en": button_key.title()}))
        session.add(
            UIButton(
                button_key=button_key,
                menu_scope=MenuScope.MAIN.value,
                row_index=row_idx,
                col_index=col_idx,
                color_emoji=emoji,
                color_hex=hex_color,
                is_active=True,
                labels_json=labels,
            )
        )
        inserted += 1
    return inserted


async def seed_app_settings(session: AsyncSession) -> int:
    """Insert default application settings idempotently."""
    settings = get_settings()
    defaults: list[tuple[str, str, str]] = [
        (
            SettingKey.USD_TO_TOMAN_RATE.value,
            str(settings.USD_TO_TOMAN_RATE),
            "USD to Iranian Toman (IRT) conversion rate",
        ),
        (
            SettingKey.RATE_MODE.value,
            "manual",
            "Exchange rate source mode: manual or live",
        ),
        (
            SettingKey.MAINTENANCE_MODE.value,
            "false",
            "Global bot maintenance mode toggle (true/false)",
        ),
        (
            SettingKey.PINNED_LIMIT.value,
            "4",
            "Maximum admin-pinned featured product buttons shown above main menu",
        ),
        (
            SettingKey.LOW_STOCK_DEFAULT_THRESHOLD.value,
            "5",
            "Default inventory threshold for dashboard low-stock alerts",
        ),
    ]
    existing = set((await session.execute(select(AppSetting.key))).scalars().all())
    inserted = 0
    for key, value, desc in defaults:
        if key not in existing:
            session.add(AppSetting(key=key, value=value, description=desc))
            inserted += 1
    return inserted


async def seed_sample_catalog(session: AsyncSession) -> int:
    """Seed starter categories and products if catalog is currently empty."""
    existing_cat = (await session.execute(select(Category.id).limit(1))).scalar_one_or_none()
    if existing_cat is not None:
        return 0

    ai_cat = Category(
        slug="ai-subscriptions",
        sort_order=1,
        is_active=True,
        color_emoji="🔵",
        color_hex="#3b82f6",
        names_json={
            "fa": "✨ اشتراک‌های هوش مصنوعی",
            "en": "✨ AI Subscriptions",
            "ar": "✨ اشتراكات الذكاء الاصطناعي",
            "hi": "✨ AI सब्सक्रिप्शन",
            "ru": "✨ ИИ-подписки",
            "de": "✨ KI-Abonnements",
            "zh": "✨ AI 会员订阅",
            "es": "✨ Suscripciones IA",
            "pt": "✨ Assinaturas de IA",
            "id": "✨ Langganan AI",
            "fr": "✨ Abonnements IA",
            "tr": "✨ Yapay Zeka Abonelikleri",
        },
        descriptions_json={
            "fa": "اکانت‌ها و فعال‌سازی سرویس‌های هوش مصنوعی",
            "en": "Premium AI accounts and instant digital activations.",
            "ar": "حسابات وتفعيلات الذكاء الاصطناعي المميزة.",
            "hi": "प्रीमियम AI खाते और त्वरित सक्रियण।",
            "ru": "Премиум-аккаунты и активации ИИ-сервисов.",
            "de": "Premium-KI-Konten und schnelle Aktivierungen.",
            "zh": "精选 AI 高级会员账号与代充激活服务。",
            "es": "Cuentas premium de IA y activaciones digitales.",
            "pt": "Contas premium de IA e ativações digitais.",
            "id": "Akun AI premium dan aktivasi digital cepat.",
            "fr": "Comptes IA premium et activations rapides.",
            "tr": "Premium yapay zeka hesapları ve hızlı aktivasyon.",
        },
    )
    creative_cat = Category(
        slug="creative-tools",
        sort_order=2,
        is_active=True,
        color_emoji="🟣",
        color_hex="#a855f7",
        names_json={
            "fa": "🎬 ابزارهای طراحی و آموزش",
            "en": "🎬 Creative & Learning",
            "ar": "🎬 أدوات التصميم والتعلم",
            "hi": "🎬 क्रिएटिव और लर्निंग",
            "ru": "🎬 Творчество и обучение",
            "de": "🎬 Kreativ & Lernen",
            "zh": "🎬 创意与学习工具",
            "es": "🎬 Creatividad y aprendizaje",
            "pt": "🎬 Criatividade e aprendizado",
            "id": "🎬 Kreatif & Belajar",
            "fr": "🎬 Création & Apprentissage",
            "tr": "🎬 Tasarım ve Eğitim",
        },
        descriptions_json={
            "fa": "ابزارهای تدوین، طراحی و یادگیری زبان",
            "en": "Video editing, design, and language learning memberships.",
            "ar": "اشتراكات المونتاج والتصميم وتعلم اللغات.",
            "hi": "वीडियो संपादन, डिज़ाइन और भाषा सीखने की सदस्यता।",
            "ru": "Сервисы для монтажа, дизайна и изучения языков.",
            "de": "Video-, Design- und Sprachlern-Abonnements.",
            "zh": "视频剪辑、创意设计与语言学习会员。",
            "es": "Membresías de edición, diseño e idiomas.",
            "pt": "Assinaturas de edição, design e idiomas.",
            "id": "Langganan edit video, desain, dan belajar bahasa.",
            "fr": "Abonnements vidéo, design et langues.",
            "tr": "Video düzenleme, tasarım ve dil öğrenme abonelikleri.",
        },
    )
    session.add_all([ai_cat, creative_cat])
    await session.flush()

    gemini_prod = Product(
        category_id=ai_cat.id,
        slug="gemini-pro-12m",
        delivery_type=DeliveryType.MANUAL.value,
        manual_prompt_key="shop.manual_input_prompt",
        price_usd=Decimal("9.99000000"),
        price_irt=Decimal("650000.00"),
        color_emoji="🟢",
        color_hex="#22c55e",
        is_pinned=True,
        is_active=True,
        sort_order=1,
        manual_stock_count=-1,
        low_stock_threshold=5,
        sold_count=0,
        titles_json={
            "fa": "🚀 جمینای پرو ۱۲ ماهه",
            "en": "🚀 GEMINI PRO 12 MONTHS",
            "ar": "🚀 جيميني برو ١٢ شهراً",
            "hi": "🚀 जेमिनी प्रो 12 महीने",
            "ru": "🚀 GEMINI PRO 12 МЕСЯЦЕВ",
            "de": "🚀 GEMINI PRO 12 MONATE",
            "zh": "🚀 GEMINI PRO 12个月会员",
            "es": "🚀 GEMINI PRO 12 MESES",
            "pt": "🚀 GEMINI PRO 12 MESES",
            "id": "🚀 GEMINI PRO 12 BULAN",
            "fr": "🚀 GEMINI PRO 12 MOIS",
            "tr": "🚀 GEMINI PRO 12 AY",
        },
        descriptions_json={
            "fa": "فعال‌سازی رسمی اشتراک ۱۲ ماهه روی ایمیل شخصی شما.",
            "en": "Official 12-month activation delivered directly to your account.",
            "ar": "تفعيل رسمي لمدة 12 شهراً على حسابك مباشرة.",
            "hi": "आपके खाते पर सीधा आधिकारिक 12-माह सक्रियण।",
            "ru": "Официальная активация подписки на 12 месяцев на ваш аккаунт.",
            "de": "Offizielle 12-Monats-Aktivierung direkt für Ihr Konto.",
            "zh": "官方正版 12 个月会员，直接激活至您的账号。",
            "es": "Activación oficial de 12 meses directamente en tu cuenta.",
            "pt": "Ativação oficial de 12 meses diretamente na sua conta.",
            "id": "Aktivasi resmi 12 bulan langsung ke akun Anda.",
            "fr": "Activation officielle de 12 mois directement sur votre compte.",
            "tr": "Hesabınıza doğrudan 12 aylık resmi aktivasyon.",
        },
        rules_json={
            "fa": "• لطفاً ایمیل معتبر ارسال کنید.\n• زمان انجام سفارش: ۵ تا ۳۰ دقیقه.",
            "en": "• Provide a valid account email.\n• Processing time: 5–30 minutes.",
            "ar": "• يرجى إرسال بريد إلكتروني صحيح.\n• وقت التنفيذ: 5 إلى 30 دقيقة.",
            "hi": "• मान्य ईमेल प्रदान करें।\n• प्रोसेसिंग समय: 5–30 मिनट।",
            "ru": "• Укажите действующий email.\n• Время выполнения: 5–30 минут.",
            "de": "• Bitte gültige E-Mail angeben.\n• Bearbeitungszeit: 5–30 Minuten.",
            "zh": "• 请提供有效的账号邮箱。\n• 预计处理时间：5–30 分钟。",
            "es": "• Ingresa un correo válido.\n• Tiempo de entrega: 5–30 minutos.",
            "pt": "• Informe um e-mail válido.\n• Prazo de entrega: 5–30 minutos.",
            "id": "• Masukkan email akun yang valid.\n• Waktu proses: 5–30 menit.",
            "fr": "• Fournissez un e-mail valide.\n• Délai de traitement : 5–30 minutes.",
            "tr": "• Geçerli bir e-posta adresi girin.\n• İşlem süresi: 5–30 dakika.",
        },
        manual_prompts_json={},
    )
    chatgpt_prod = Product(
        category_id=ai_cat.id,
        slug="chatgpt-plus-1m",
        delivery_type=DeliveryType.AUTO.value,
        manual_prompt_key=None,
        price_usd=Decimal("18.50000000"),
        price_irt=Decimal("1200000.00"),
        color_emoji="🟡",
        color_hex="#eab308",
        is_pinned=False,
        is_active=True,
        sort_order=2,
        manual_stock_count=0,
        low_stock_threshold=5,
        sold_count=0,
        titles_json={
            "fa": "💬 اکانت آماده ChatGPT Plus",
            "en": "💬 ChatGPT Plus Ready Account",
            "ar": "💬 حساب ChatGPT Plus جاهز",
            "hi": "💬 ChatGPT Plus तैयार खाता",
            "ru": "💬 Готовый аккаунт ChatGPT Plus",
            "de": "💬 ChatGPT Plus Konto",
            "zh": "💬 ChatGPT Plus 成品账号",
            "es": "💬 Cuenta ChatGPT Plus lista",
            "pt": "💬 Conta ChatGPT Plus pronta",
            "id": "💬 Akun Siap Pakai ChatGPT Plus",
            "fr": "💬 Compte ChatGPT Plus prêt",
            "tr": "💬 ChatGPT Plus Hazır Hesap",
        },
        descriptions_json={
            "fa": "تحویل آنی و خودکار پس از خرید.",
            "en": "Instant automatic credential delivery upon payment.",
            "ar": "تسليم فوري وتلقائي فور إتمام الشراء.",
            "hi": "भुगतान के तुरंत बाद स्वचालित डिलीवरी।",
            "ru": "Мгновенная автоматическая выдача сразу после оплаты.",
            "de": "Sofortige automatische Auslieferung nach Kauf.",
            "zh": "付款后系统全自动秒发账号凭证。",
            "es": "Entrega automática instantánea tras el pago.",
            "pt": "Entrega automática instantânea após o pagamento.",
            "id": "Pengiriman otomatis instan setelah pembayaran.",
            "fr": "Livraison automatique instantanée après paiement.",
            "tr": "Ödeme sonrası anında otomatik teslimat.",
        },
        rules_json={
            "fa": "• پس از تحویل رمز عبور را تغییر دهید.",
            "en": "• Change the password immediately after login.",
            "ar": "• يرجى تغيير كلمة المرور فور تسجيل الدخول.",
            "hi": "• लॉगिन के तुरंत बाद पासवर्ड बदलें।",
            "ru": "• Смените пароль сразу после входа.",
            "de": "• Ändern Sie das Passwort sofort nach dem Login.",
            "zh": "• 登录后请第一时间修改密码。",
            "es": "• Cambia la contraseña inmediatamente al iniciar sesión.",
            "pt": "• Altere a senha imediatamente após entrar.",
            "id": "• Segera ubah kata sandi setelah login.",
            "fr": "• Modifiez le mot de passe dès la connexion.",
            "tr": "• Giriş yaptıktan hemen sonra şifreyi değiştirin.",
        },
        manual_prompts_json={},
    )
    session.add_all([gemini_prod, chatgpt_prod])
    await session.flush()

    session.add_all(
        [
            ProductBulkTier(
                product_id=gemini_prod.id,
                min_qty=10,
                unit_price_usd=Decimal("8.99000000"),
                unit_price_irt=Decimal("585000.00"),
            ),
            ProductBulkTier(
                product_id=gemini_prod.id,
                min_qty=30,
                unit_price_usd=Decimal("7.99000000"),
                unit_price_irt=Decimal("520000.00"),
            ),
            ProductBulkTier(
                product_id=chatgpt_prod.id,
                min_qty=10,
                unit_price_usd=Decimal("17.00000000"),
                unit_price_irt=Decimal("1105000.00"),
            ),
        ]
    )
    return 2


async def run_seed(session: AsyncSession) -> dict[str, int]:
    """Execute all seed tasks inside a single database transaction."""
    async with session.begin():
        translations_added = await seed_translations(session)
        buttons_added = await seed_ui_buttons(session)
        settings_added = await seed_app_settings(session)
        products_added = await seed_sample_catalog(session)
    return {
        "translations_added": translations_added,
        "buttons_added": buttons_added,
        "settings_added": settings_added,
        "products_added": products_added,
    }


async def _main() -> None:
    configure_logging()
    init_db()
    factory = get_session_factory()
    try:
        async with factory() as session:
            summary = await run_seed(session)
            logger.info("Database seed completed: %s", summary)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(_main())
