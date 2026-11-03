"""Explicit decoder compatibility for one pre-upgrade native archive bundle.

Archived bytes and all producer files must still match their recorded digests.
Only the unchanged pure decoder is used; old producer code is never executed.
"""
import ast
import hashlib
import inspect

from config import settings

LEGACY_VERSION = 'daily-publication-archive-v1'
LEGACY_CODE = {'core/daily_publications.py': '4a743a17499ad8ef46dceb4a006edc444b6357920c4454a28056baf4c3821ccc', 'core/price_quality.py': 'bd9210fcd6ef37fb16d5cb416cc2b3c0233925af3e8c1b7b8c22b3c60143d2af', 'core/price_revision.py': '9de27f0516fa3d1fa9e3651e89ed58608a761541ea6b3bea093887f75d95eb15', 'core/client_quality.py': '3a07ee60fc772009cb7f5bc6a10505729286ae74497865bbfd6c8c61c25159a3', 'core/text.py': '8414352efc4389a2e8b9355d2e1bcb72fe7051ce13fda05284de01c97c187e2b', 'core/timeutils.py': 'fc5a382c16ed38695b41bf5967aceec878c4975a6e43f9642fcb172335ac088c', 'data_collector/base.py': '45c3fd9abe73a1bb45164855fde4ffd2b9676dfbeee022dd0f6bed8d602bc3b2', 'data_collector/tsetmc.py': 'f0ef6fec31b5fa6218238be248d1cbe218247bf487ec09a36d393e96abeb470f', 'data_collector/daily_publications.py': 'bc21f6901b85ffd9c625e7466494aab678786ef9d93ce25cbf29f1bd83f0fae0', 'data_collector/scheduler.py': '0e62ce629c9e7d7849b381d1e77ecc2e217e5fe97621221b1e9de2351765855f', 'database/db.py': 'f0959823401f544d78452892d4c3d6f0889d3937bdf0efa76be86de4577e97b9', 'database/price_recovery.py': '26da1f7134ac65548a0cad8611527fd316a93749cd5406c5f5debb11bdc2bbac', 'config/settings.py': '577bbd102879c74360911eed1ad86d1e22039c236a2a785d642a496aba75ff68', 'config/default.yaml': 'c840f631b01e467b2380906b559e90c0a816dd19571c820e698bc8cdb685ad9d'}
LEGACY_AST = {'_decode': 'f8ac6200c5ce953b6f3036ac7b95eca08d7a2206e0ffdf36cfae7cc413ff154b', '_parsed': '2e16b8f5e21ed91fec5f9738a0e0971fe78bf7afa31ca4ca04210a59b9cab302'}
LEGACY_META = ('source', 'ins_code', 'url', 'requested_at', 'received_at', 'response_bytes', 'body_sha256', 'price_unit', 'requested_date', 'kind')

# Exact producer identity from the v2 bundles created immediately before the
# order-book quarantine change. This never authorizes an unknown code vintage.
PRE_BOOK_V2_CODE = dict(LEGACY_CODE) | {
    'data_collector/tsetmc.py': 'd2695d815618f263d817fd00303258b5f5743824ca4bb839067c2c1a9a1fe6b6',
    'data_collector/daily_publications.py': '384b072a3d21566a466d01d526c79bc925cc3a96072606f3ab0514920a79e3e7',
    'core/source_cooldown.py': '7607ff9a681f7abaed13c53571b9a0d70afa01eee1ede460f2ef8414f5df22e0',
    'data_collector/source_cooldown.py': 'dad2bf40d95043edbcda61381196c09106deafe0706acbf28f9e4a248af5cbd5',
    'data_collector/publication_compat.py': 'f370ecef0d8a8c1da0e022c879d9551ea45d3b674a0d75bbf1e3a19c3c2dc934',
}
# Exact, independently recorded producer identity after the order-book change
# and before the local Windows source-state write retry. Do not substitute the
# currently installed producer hashes: unknown archive vintages must fail closed.
POST_BOOK_V2_CODE = dict(PRE_BOOK_V2_CODE) | {
    'data_collector/daily_publications.py': '56bfcd4e8b87d7f86541c0f6a7bc3c459d04511678f0b564db6124a3107e05bc',
    'data_collector/scheduler.py': '1b69f4496ba69deb07608c0f18cd22778d6574b63b4664064448dfd5eb606583',
    'data_collector/publication_compat.py': '71611665a42ed9f9c583b1eb602777630bf9987db4cb0697150707f58470c166',
    'core/order_book.py': '014a68e1cb1be2ab2021c7201f510451cde701e1c2a765b354ef28c8d0483d72',
}
# Exact producer identity of the byte-verified 2026-11-02 price archive,
# before the date-scoped source circuit breaker. This identity was recorded
# by the producer and is not constructed from currently installed file hashes.
PRE_DATE_CIRCUIT_V2_CODE = dict(POST_BOOK_V2_CODE) | {
    'data_collector/base.py': '174e5a147b90015a2fedb3c288dc9feed7e588a14f0ce679fdb1ca29afe03dc7',
    'data_collector/tsetmc.py': '12efdb0f985292f42d04acfed3d8b706c9f673fe73af860049bb66b1436050d3',
    'data_collector/scheduler.py': '168eb59e34377ac8fc35c8812994c77a8376fbe9394f33f660835a4ee1688431',
    'database/db.py': 'c88fc486f552b8075531ef61e2da1e91404f0a7b0cfc11cded2b6f9f013c88e4',
    'data_collector/source_cooldown.py': '163e43c2bd2f6bc2488c6242e6e489a206636ab7b0578141194c4d0df58e0bb1',
    'data_collector/publication_compat.py': 'c68102cfed8fff2f9bd0aec5c6f5ed86ebc3d3babaa99e335836aab9388ff695',
}
# Exact producer of the 24-row price check, before reporting incomplete
# normalized history as partial. Its pure replay decoder is unchanged.
PRE_HISTORY_QUALITY_V2_CODE = dict(PRE_DATE_CIRCUIT_V2_CODE) | {
    'data_collector/daily_publications.py': '0f7747e26e126d847a136edad3e0b82bf2ee6905f511806a6dff81fa51e76283',
    'data_collector/publication_compat.py': 'cb1bb42b660b4276c542c3828c86b8fb718cbf7ab46e45eb826f865227ccb723',
}
# This exact replacement only adds bounded retries to local state writes;
# checked_transport_evidence and the native archive decoder are unchanged.
WINDOWS_RETRY_COOLDOWN_SHA256 = '163e43c2bd2f6bc2488c6242e6e489a206636ab7b0578141194c4d0df58e0bb1'
PRE_BOOK_V2_AST = dict(LEGACY_AST) | {
    'checked_transport_clock': '47321c207de78273b494cf5802c4408cb81d0e2bdaa5f2ee164ef3a9e24c0e1d',
    '_tasks': 'ae7af543d034dc81a444e1fa1f6188b542ce95b66a01e07a3e9e7e8a0a322a39',
    '_sha': '8fe8ddd93310643be0b0e89b3cea6c59eb2f1c8b0cf746f35a1ee6f544a0480c',
    'review_archive': '6acef53779de70e5018621c47e8d84f5101f46b046c1edd8159964e76cac949d',
}


def archive_contract(version, recorded_code):
    from data_collector import daily_publications as archive

    if not isinstance(recorded_code, dict):
        raise ValueError('Explicit archived source code identity is required')
    if version == archive.VERSION and recorded_code == archive._code():
        return tuple(recorded_code)
    if version == LEGACY_VERSION and recorded_code == LEGACY_CODE:
        decoder_ast, extra_files = LEGACY_AST, ()
    elif version == archive.VERSION and recorded_code in (
            PRE_BOOK_V2_CODE, POST_BOOK_V2_CODE, PRE_DATE_CIRCUIT_V2_CODE, PRE_HISTORY_QUALITY_V2_CODE):
        decoder_ast = PRE_BOOK_V2_AST
        extra_files = ('core/source_cooldown.py', 'data_collector/source_cooldown.py')
    else:
        raise ValueError('Unknown or incompatible dated-publication source code')
    # These determine native numeric/date semantics and must remain byte-identical.
    parser_files = ('core/daily_publications.py', 'core/price_quality.py', 'core/price_revision.py',
                    'core/client_quality.py', 'core/text.py', 'core/timeutils.py', *extra_files)
    for name in parser_files:
        body = (settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')
        current = hashlib.sha256(body).hexdigest()
        allowed_retry = (name == 'data_collector/source_cooldown.py'
                         and recorded_code in (PRE_BOOK_V2_CODE, POST_BOOK_V2_CODE)
                         and current == WINDOWS_RETRY_COOLDOWN_SHA256)
        if current != recorded_code[name] and not allowed_retry:
            raise ValueError('Legacy archive pure decoder dependencies changed')
    if archive.META != LEGACY_META:
        raise ValueError('Legacy archive metadata interpretation changed')
    for name, expected in decoder_ast.items():
        body = ast.dump(ast.parse(inspect.getsource(getattr(archive,name))),include_attributes=False).encode()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError('Legacy archive native decoding function changed')
    return tuple(recorded_code)
